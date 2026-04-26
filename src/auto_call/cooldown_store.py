"""In-memory cooldown tracking for auto-call dispatch.

Single instance per relay process (LXC, no clustering needed for v1).
"""
import time
from threading import Lock
from typing import Dict, Tuple


class CooldownStore:
    def __init__(self) -> None:
        self._last: Dict[Tuple[str, str], float] = {}
        self._lock = Lock()

    def is_hot(self, device_token: str, camera_id: str, cooldown_seconds: int) -> bool:
        """True if a call was dispatched recently and we should suppress."""
        if cooldown_seconds <= 0:
            return False
        key = (device_token, camera_id)
        with self._lock:
            last = self._last.get(key)
        if last is None:
            return False
        return (time.monotonic() - last) < cooldown_seconds

    def mark_hot(self, device_token: str, camera_id: str) -> None:
        key = (device_token, camera_id)
        with self._lock:
            self._last[key] = time.monotonic()

    def clear(self, device_token: str | None = None) -> None:
        with self._lock:
            if device_token is None:
                self._last.clear()
            else:
                self._last = {
                    k: v for k, v in self._last.items() if k[0] != device_token
                }
