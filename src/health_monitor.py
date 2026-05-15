"""Health monitoring state for the relay.

Tracks MQTT connection status, event metrics, and uptime for the /health endpoint.
Thread-safe via asyncio (single event loop, no locks needed).
"""
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class HealthState:
    """Shared health state updated by the main MQTT loop."""

    # MQTT connection status
    mqtt_connected: bool = False
    mqtt_host: str = "unknown"
    mqtt_port: int = 1883
    mqtt_last_connected: Optional[float] = None
    mqtt_last_disconnected: Optional[float] = None

    # Event metrics
    events_received: int = 0
    events_filtered: int = 0
    events_delivered: int = 0
    events_failed: int = 0
    last_event_time: Optional[float] = None
    last_event_camera: Optional[str] = None
    last_event_label: Optional[str] = None

    # Uptime
    start_time: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        """Serialize to JSON-compatible dict for /health endpoint."""
        now = time.time()
        uptime = now - self.start_time

        # Calculate time since last event (if any)
        last_event_ago = None
        if self.last_event_time:
            last_event_ago = int(now - self.last_event_time)

        # Calculate connection duration
        mqtt_connected_for = None
        if self.mqtt_connected and self.mqtt_last_connected:
            mqtt_connected_for = int(now - self.mqtt_last_connected)

        return {
            "status": "ok" if self.mqtt_connected else "degraded",
            "uptime_seconds": int(uptime),
            "mqtt": {
                "connected": self.mqtt_connected,
                "host": self.mqtt_host,
                "port": self.mqtt_port,
                "connected_for_seconds": mqtt_connected_for,
                "last_connected_at": (
                    datetime.fromtimestamp(self.mqtt_last_connected).isoformat()
                    if self.mqtt_last_connected else None
                ),
                "last_disconnected_at": (
                    datetime.fromtimestamp(self.mqtt_last_disconnected).isoformat()
                    if self.mqtt_last_disconnected else None
                ),
            },
            "events": {
                "total_received": self.events_received,
                "filtered": self.events_filtered,
                "delivered": self.events_delivered,
                "failed": self.events_failed,
                "last_event_ago_seconds": last_event_ago,
                "last_event_camera": self.last_event_camera,
                "last_event_label": self.last_event_label,
                "last_event_at": (
                    datetime.fromtimestamp(self.last_event_time).isoformat()
                    if self.last_event_time else None
                ),
            },
        }

    def mark_mqtt_connected(self, host: str, port: int):
        """Called when MQTT connection succeeds."""
        self.mqtt_connected = True
        self.mqtt_host = host
        self.mqtt_port = port
        self.mqtt_last_connected = time.time()

    def mark_mqtt_disconnected(self):
        """Called when MQTT connection drops."""
        self.mqtt_connected = False
        self.mqtt_last_disconnected = time.time()

    def record_event_received(self, camera: str, label: str):
        """Called when an event is parsed from MQTT (before filtering)."""
        self.events_received += 1
        self.last_event_time = time.time()
        self.last_event_camera = camera
        self.last_event_label = label

    def record_event_filtered(self, reason: str):
        """Called when an event is suppressed by filters or cooldown."""
        self.events_filtered += 1

    def record_event_delivered(self):
        """Called when a push is successfully delivered."""
        self.events_delivered += 1

    def record_event_failed(self):
        """Called when a push delivery fails."""
        self.events_failed += 1


# Global instance (single event loop, no locks needed)
health_state = HealthState()
