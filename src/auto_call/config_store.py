"""Disk-backed AutoCallConfig store, refreshed on file change.

Mirrors the iOS AutoCallConfig schema. The Phase 2.C config-sync REST endpoint
writes to this file; Phase 2.B reads it at startup and on SIGHUP / mtime change.
"""
import json
import os
import threading
from dataclasses import dataclass, field
from typing import Dict, List, Optional


CONFIG_PATH = os.environ.get(
    "LUMEN_AUTOCALL_CONFIG_PATH",
    "/var/lib/lumen-push-relay/auto_call_configs.json",
)


@dataclass
class AutoCallConfig:
    enabled: bool = False
    trigger: str = "zoneDetection"  # physicalButton | zoneDetection | both
    required_zone_id: Optional[str] = None
    min_score: float = 0.85
    cooldown_seconds: int = 60
    required_objects: List[str] = field(default_factory=lambda: ["person"])
    manual_call_enabled: bool = False
    # Schedule fields are optional and only relevant when enabled=true.
    schedule: Optional[dict] = None


@dataclass
class TokenConfig:
    voip_token: str  # hex
    auth_header: Optional[str] = None  # e.g. "Basic xxxx" forwarded to go2rtc
    server_url: Optional[str] = None  # for snapshot URL composition
    cameras: Dict[str, AutoCallConfig] = field(default_factory=dict)


class ConfigStore:
    """Map<deviceTokenHex, TokenConfig>. Thread-safe."""

    def __init__(self, path: str = CONFIG_PATH) -> None:
        self._path = path
        self._tokens: Dict[str, TokenConfig] = {}
        self._lock = threading.Lock()
        self._mtime: Optional[float] = None
        self.reload()

    def reload(self) -> None:
        try:
            mtime = os.path.getmtime(self._path)
        except FileNotFoundError:
            return
        if self._mtime == mtime:
            return
        try:
            with open(self._path, "r") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            return  # keep last good cache
        new = {}
        for token, raw in data.items():
            cameras = {}
            for cam, cfg in (raw.get("cameras") or {}).items():
                cameras[cam] = AutoCallConfig(
                    enabled=bool(cfg.get("enabled", False)),
                    trigger=str(cfg.get("trigger", "zoneDetection")),
                    required_zone_id=cfg.get("requiredZoneId"),
                    min_score=float(cfg.get("minScore", 0.85)),
                    cooldown_seconds=int(cfg.get("cooldownSeconds", 60)),
                    required_objects=list(cfg.get("requiredObjects", ["person"])),
                    manual_call_enabled=bool(cfg.get("manualCallEnabled", False)),
                    schedule=cfg.get("schedule"),
                )
            new[token] = TokenConfig(
                voip_token=token,
                auth_header=raw.get("authHeader"),
                server_url=raw.get("serverURL"),
                cameras=cameras,
            )
        with self._lock:
            self._tokens = new
            self._mtime = mtime

    def matching_dispatches(self, event: dict) -> List[tuple[TokenConfig, str, AutoCallConfig]]:
        """Return all (token, cameraId, config) that should fire for this event."""
        # event is the parsed Frigate MQTT 'after' payload (dict with `camera`, `label`,
        # `top_score`, `current_zones`).
        camera = event.get("camera")
        if not camera:
            return []
        out = []
        with self._lock:
            tokens = list(self._tokens.values())
        for tc in tokens:
            cfg = tc.cameras.get(camera)
            if cfg is None or not cfg.enabled:
                continue
            label = event.get("label")
            if label and cfg.required_objects and label not in cfg.required_objects:
                continue
            score = float(event.get("top_score", 0.0))
            if score < cfg.min_score:
                continue
            zones = event.get("current_zones") or []
            if cfg.required_zone_id and cfg.required_zone_id not in zones:
                continue
            # schedule check
            if cfg.schedule and not _within_schedule(cfg.schedule):
                continue
            out.append((tc, camera, cfg))
        return out


def _within_schedule(schedule: dict) -> bool:
    """schedule has startHour/startMinute/endHour/endMinute/weekdaysOnly."""
    import datetime as dt
    now = dt.datetime.now()
    if schedule.get("weekdaysOnly") and now.weekday() >= 5:
        return False
    sh = int(schedule.get("startHour", 0))
    sm = int(schedule.get("startMinute", 0))
    eh = int(schedule.get("endHour", 23))
    em = int(schedule.get("endMinute", 59))
    start = now.replace(hour=sh, minute=sm, second=0, microsecond=0)
    end = now.replace(hour=eh, minute=em, second=0, microsecond=0)
    if start <= end:
        return start <= now < end
    # overnight wrap
    return now >= start or now < end
