"""Disk-backed config store for cameras + buttons, mtime-polled.

Schema (auto_call_configs.json):
{
  "<deviceTokenHex>": {
    "voipToken": "<hex>", "serverURL": "...", "authHeader": "...",
    "cameras":  { "<cameraId>": { ...AutoCallConfig... } },
    "buttons":  [ { ...PhysicalButtonBinding... }, ... ]
  }
}
"""
import json
import os
import threading
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .buttons.models import PhysicalButtonBinding, parse_binding


CONFIG_PATH = os.environ.get(
    "LUMEN_AUTOCALL_CONFIG_PATH",
    "/var/lib/lumen-push-relay/auto_call_configs.json",
)


@dataclass
class AutoCallConfig:
    enabled: bool = False
    trigger: str = "zoneDetection"
    required_zone_id: Optional[str] = None
    min_score: float = 0.85
    cooldown_seconds: int = 60
    required_objects: List[str] = field(default_factory=lambda: ["person"])
    manual_call_enabled: bool = False
    schedule: Optional[dict] = None


@dataclass
class TokenConfig:
    voip_token: str
    auth_header: Optional[str] = None
    server_url: Optional[str] = None
    cameras: Dict[str, AutoCallConfig] = field(default_factory=dict)
    buttons: List[PhysicalButtonBinding] = field(default_factory=list)


DEFAULT_BUTTON_COOLDOWN_S = 60


class ConfigStore:
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
            return
        new: Dict[str, TokenConfig] = {}
        for token, raw in data.items():
            cameras: Dict[str, AutoCallConfig] = {}
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
            buttons: List[PhysicalButtonBinding] = []
            for raw_b in (raw.get("buttons") or []):
                try:
                    buttons.append(parse_binding(raw_b))
                except Exception:
                    continue
            new[token] = TokenConfig(
                voip_token=token,
                auth_header=raw.get("authHeader"),
                server_url=raw.get("serverURL"),
                cameras=cameras,
                buttons=buttons,
            )
        with self._lock:
            self._tokens = new
            self._mtime = mtime

    # ---- existing zone-detection lookup (unchanged behaviour) ----

    def matching_dispatches(self, event: dict):
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
            if cfg.schedule and not _within_schedule(cfg.schedule):
                continue
            out.append((tc, camera, cfg))
        return out

    # ---- new button lookup ----

    def bindings_matching_topic(self, topic: str) -> list[tuple[str, PhysicalButtonBinding]]:
        """Return all (token, binding) where binding.topic == topic."""
        out: list[tuple[str, PhysicalButtonBinding]] = []
        with self._lock:
            tokens = list(self._tokens.values())
        for tc in tokens:
            for b in tc.buttons:
                if b.topic == topic:
                    out.append((tc.voip_token, b))
        return out

    def all_button_topics(self) -> set[str]:
        """Union of every binding.topic across all tokens. Used by SubscriptionManager."""
        with self._lock:
            tokens = list(self._tokens.values())
        out: set[str] = set()
        for tc in tokens:
            for b in tc.buttons:
                if b.topic:
                    out.add(b.topic)
        return out

    def camera_cooldown_for(self, token: str, camera_id: str) -> int:
        """Cooldown seconds for a (token, camera) pair, defaulting to 60s."""
        with self._lock:
            tc = self._tokens.get(token)
            if tc is None:
                return DEFAULT_BUTTON_COOLDOWN_S
            cfg = tc.cameras.get(camera_id)
            if cfg is None:
                return DEFAULT_BUTTON_COOLDOWN_S
            return cfg.cooldown_seconds


def _within_schedule(schedule: dict) -> bool:
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
    return now >= start or now < end
