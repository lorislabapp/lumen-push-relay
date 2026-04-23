#!/usr/bin/env python3
"""Frigate Push Relay — sends push notifications for Frigate detection events.

Supports two modes:
  - "worker" mode: POSTs events to a Cloudflare Worker URL (recommended for most users)
  - "direct" mode: Signs APNs JWTs and sends directly to Apple (requires APNs key)
"""

import asyncio
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path

import aiomqtt
import httpx
import yaml

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("frigate-push")

# APNs endpoints (direct mode only)
APNS_HOST = {
    "production": "https://api.push.apple.com",
    "sandbox": "https://api.sandbox.push.apple.com",
}


class WorkerSender:
    """Sends events to the Cloudflare Worker which handles APNs delivery."""

    def __init__(self, push_url: str):
        self.push_url = push_url
        self._client = httpx.AsyncClient(timeout=10)

    async def send(self, event: dict) -> bool:
        """POST a Frigate event payload to the Worker. Returns True on success."""
        # The Worker expects the same format as Frigate MQTT: {"type":"new","after":{...}}
        payload = {"type": "new", "before": None, "after": event}
        try:
            resp = await self._client.post(
                self.push_url,
                json=payload,
                headers={"Content-Type": "application/json"},
            )
            if resp.status_code == 200:
                return True
            log.error("Worker error %d: %s", resp.status_code, resp.text)
            return False
        except Exception as e:
            log.error("Worker request failed: %s", e)
            return False

    async def close(self):
        await self._client.aclose()


class DirectAPNsSender:
    """Sends push notifications directly via Apple's HTTP/2 APNs API."""

    def __init__(self, key_file: str, key_id: str, team_id: str, bundle_id: str, environment: str, device_tokens: list[str]):
        import jwt as pyjwt
        self._pyjwt = pyjwt
        self.key_id = key_id
        self.team_id = team_id
        self.bundle_id = bundle_id
        self.host = APNS_HOST[environment]
        self.private_key = Path(key_file).read_text()
        self.device_tokens = device_tokens
        self._token: str | None = None
        self._token_time: float = 0
        self._client = httpx.AsyncClient(http2=True, timeout=10)

    def _get_token(self) -> str:
        now = time.time()
        if self._token and (now - self._token_time) < 3000:
            return self._token
        self._token = self._pyjwt.encode(
            {"iss": self.team_id, "iat": int(now)},
            self.private_key,
            algorithm="ES256",
            headers={"kid": self.key_id},
        )
        self._token_time = now
        log.info("Refreshed APNs JWT token")
        return self._token

    async def send(self, event: dict) -> bool:
        payload = build_apns_payload(event)
        # apns-expiration: retry for 1h on transient delivery failures.
        # A value of "0" tells APNs to drop the push immediately if the device
        # is offline/asleep — that's the cause of "missed notification" reports.
        expires_at = int(time.time()) + 3600
        headers = {
            "authorization": f"bearer {self._get_token()}",
            "apns-topic": self.bundle_id,
            "apns-push-type": "alert",
            "apns-priority": "10",
            "apns-expiration": str(expires_at),
        }
        success = False
        for token in self.device_tokens:
            url = f"{self.host}/3/device/{token}"
            try:
                resp = await self._client.post(url, json=payload, headers=headers)
                if resp.status_code == 200:
                    log.info("Push delivered to %s...%s", token[:8], token[-8:])
                    success = True
                else:
                    log.error("APNs error %d for %s...%s: %s", resp.status_code, token[:8], token[-8:], resp.text)
            except Exception as e:
                log.error("APNs request failed for %s...%s: %s", token[:8], token[-8:], e)
        return success

    async def close(self):
        await self._client.aclose()


class CooldownTracker:
    """Per camera+label cooldown to avoid notification spam."""

    def __init__(self, cooldown_seconds: int):
        self.cooldown = cooldown_seconds
        self._last: dict[str, float] = {}

    def check_and_claim(self, camera: str, label: str) -> bool:
        key = f"{camera}/{label}"
        now = time.time()
        last = self._last.get(key, 0)
        if now - last < self.cooldown:
            return False
        self._last[key] = now
        return True


def build_apns_payload(event: dict) -> dict:
    """Build an APNs notification payload from a Frigate event (direct mode only)."""
    camera = event.get("camera", "unknown")
    label = event.get("label", "unknown")
    score = event.get("top_score", 0)
    zones = event.get("zones", [])
    sub_label = event.get("sub_label")

    camera_display = camera.replace("_", " ").title()

    body_parts = []
    if zones:
        body_parts.append(f"Zone: {', '.join(zones)}")
    if score:
        body_parts.append(f"Confidence: {int(score * 100)}%")
    if sub_label:
        body_parts.append(sub_label)
    body = " | ".join(body_parts) if body_parts else camera_display

    # Use custom zone message if present
    title = event.get("custom_title") or f"{label.title()} Detected"
    body = event.get("custom_body") or body

    return {
        "aps": {
            "alert": {
                "title": title,
                "subtitle": camera_display,
                "body": body,
            },
            "sound": "default",
            "interruption-level": "time-sensitive",
            "category": "FRIGATE_EVENT",
            "thread-id": camera,
            "mutable-content": 1,
        },
        "eventId": event.get("id", ""),
        "camera": camera,
        "label": label,
        "zones": zones,
        "source": "mqtt-relay",
    }


def _in_schedule(schedule: str) -> bool:
    """Check if current local time is within a schedule like '00:00-06:00'."""
    start_str, end_str = schedule.split("-")
    now = datetime.now().time()
    start = datetime.strptime(start_str.strip(), "%H:%M").time()
    end = datetime.strptime(end_str.strip(), "%H:%M").time()
    if start <= end:
        return start <= now <= end
    return now >= start or now <= end


def should_notify(event: dict, config: dict) -> str | None:
    """Check if an event passes filters. Returns None if OK, or a reason string if suppressed."""
    filters = config.get("filters", {})

    label = event.get("label", "")
    allowed_labels = filters.get("labels", [])
    if allowed_labels and label not in allowed_labels:
        return f"label '{label}' not in allowed list"

    score = event.get("top_score", 0)
    min_score = filters.get("min_score", 0)
    if score < min_score:
        return f"score {score:.2f} < {min_score}"

    if event.get("stationary"):
        return "stationary"

    camera = event.get("camera", "")
    zones = event.get("current_zones", []) or event.get("zones", [])
    cameras_config = filters.get("cameras", {})
    if cameras_config:
        cam_cfg = cameras_config.get(camera)
        if cam_cfg is None:
            return f"camera '{camera}' not in allowed cameras"
        if not cam_cfg.get("enabled", True):
            return f"camera '{camera}' disabled"
        cam_labels = cam_cfg.get("labels")
        if cam_labels and label not in cam_labels:
            return f"label '{label}' not allowed on camera '{camera}'"
        cam_min = cam_cfg.get("min_score")
        if cam_min and score < cam_min:
            return f"score {score:.2f} < {cam_min} for camera '{camera}'"
        required_zones = cam_cfg.get("required_zones")
        if required_zones and not set(zones) & set(required_zones):
            return f"not in required zones {required_zones} (current: {zones})"
        schedule = cam_cfg.get("schedule")
        if schedule and not _in_schedule(schedule):
            return f"camera '{camera}' outside schedule '{schedule}'"

    return None


def get_zone_message(event: dict, config: dict) -> dict | None:
    """Return custom title/body if event matches a zone_messages rule, or None."""
    camera = event.get("camera", "")
    zones = set(event.get("current_zones", []) or event.get("zones", []))
    cam_cfg = config.get("filters", {}).get("cameras", {}).get(camera, {})
    zone_messages = cam_cfg.get("zone_messages", {})
    for zone_name, msg in zone_messages.items():
        if zone_name in zones:
            return {"custom_title": msg.get("title", ""), "custom_body": msg.get("body", "")}
    return None


def load_config() -> dict:
    """Load config from file or environment variables."""
    # Check for config file
    config_path = os.environ.get("CONFIG_PATH", "/config/config.yaml")
    if Path(config_path).exists():
        return yaml.safe_load(Path(config_path).read_text())

    # Fall back to environment variables (Docker-friendly)
    config = {
        "frigate": {
            "mqtt_host": os.environ.get("MQTT_HOST", "localhost"),
            "mqtt_port": int(os.environ.get("MQTT_PORT", "1883")),
            "mqtt_user": os.environ.get("MQTT_USER"),
            "mqtt_password": os.environ.get("MQTT_PASSWORD"),
            "topic": os.environ.get("MQTT_TOPIC", "frigate/events"),
        },
        "filters": {
            "labels": os.environ.get("FILTER_LABELS", "person,car,dog,cat,package").split(","),
            "min_score": float(os.environ.get("FILTER_MIN_SCORE", "0.6")),
            "cooldown_seconds": int(os.environ.get("COOLDOWN_SECONDS", "120")),
        },
    }

    # Worker mode (recommended)
    push_url = os.environ.get("PUSH_URL")
    if push_url:
        config["mode"] = "worker"
        config["worker"] = {"push_url": push_url}
    else:
        # Direct mode
        config["mode"] = "direct"
        config["apns"] = {
            "key_file": os.environ.get("APNS_KEY_FILE", "/config/AuthKey.p8"),
            "key_id": os.environ.get("APNS_KEY_ID", ""),
            "team_id": os.environ.get("APNS_TEAM_ID", ""),
            "bundle_id": os.environ.get("APNS_BUNDLE_ID", ""),
            "environment": os.environ.get("APNS_ENVIRONMENT", "production"),
        }
        tokens = os.environ.get("DEVICE_TOKENS", "")
        config["devices"] = [{"token": t.strip()} for t in tokens.split(",") if t.strip()]

    return config


async def run():
    """Main loop: subscribe to MQTT, filter events, send pushes."""
    config = load_config()

    frigate_cfg = config["frigate"]
    cooldown_sec = config.get("filters", {}).get("cooldown_seconds", 120)

    # Determine mode
    mode = config.get("mode", "direct")
    push_url = config.get("worker", {}).get("push_url")

    if push_url or mode == "worker":
        if not push_url:
            log.error("Worker mode requires PUSH_URL — exiting")
            return
        sender = WorkerSender(push_url)
        log.info("Mode: WORKER → %s", push_url[:60] + "...")
    else:
        apns_cfg = config["apns"]
        devices = [d["token"] for d in config.get("devices", [])]
        if not devices:
            log.error("Direct mode requires device tokens — exiting")
            return
        sender = DirectAPNsSender(
            key_file=apns_cfg["key_file"],
            key_id=apns_cfg["key_id"],
            team_id=apns_cfg["team_id"],
            bundle_id=apns_cfg["bundle_id"],
            environment=apns_cfg.get("environment", "production"),
            device_tokens=devices,
        )
        log.info("Mode: DIRECT APNs → %s (%d devices)", apns_cfg.get("environment", "production"), len(devices))

    cooldown = CooldownTracker(cooldown_sec)

    log.info(
        "Connecting to MQTT %s:%d topic=%s",
        frigate_cfg["mqtt_host"],
        frigate_cfg["mqtt_port"],
        frigate_cfg["topic"],
    )

    retry_delay = 1
    while True:
        try:
            mqtt_kwargs = {
                "hostname": frigate_cfg["mqtt_host"],
                "port": frigate_cfg["mqtt_port"],
            }
            if frigate_cfg.get("mqtt_user"):
                mqtt_kwargs["username"] = frigate_cfg["mqtt_user"]
            if frigate_cfg.get("mqtt_password"):
                mqtt_kwargs["password"] = frigate_cfg["mqtt_password"]

            async with aiomqtt.Client(**mqtt_kwargs) as mqtt:
                await mqtt.subscribe(frigate_cfg["topic"])
                log.info("Connected to MQTT, listening for events...")
                retry_delay = 1

                async for message in mqtt.messages:
                    try:
                        data = json.loads(message.payload)
                    except (json.JSONDecodeError, TypeError):
                        continue

                    if data.get("type") != "new":
                        continue

                    event = data.get("after") or data.get("before")
                    if not event:
                        continue

                    camera = event.get("camera", "?")
                    label = event.get("label", "?")
                    score = event.get("top_score", 0)

                    reason = should_notify(event, config)
                    if reason:
                        log.debug("Suppressed %s on %s: %s", label, camera, reason)
                        continue

                    if not cooldown.check_and_claim(camera, label):
                        log.debug("Cooldown active for %s/%s", camera, label)
                        continue

                    # Inject custom zone message if configured
                    zone_msg = get_zone_message(event, config)
                    if zone_msg:
                        event = {**event, **zone_msg}
                        log.info(
                            "→ %s on %s (%.0f%%, zones=%s) [%s]",
                            label, camera, score * 100, event.get("zones", []),
                            zone_msg.get("custom_title", ""),
                        )
                    else:
                        log.info(
                            "→ %s on %s (%.0f%%, zones=%s)",
                            label, camera, score * 100, event.get("zones", []),
                        )

                    ok = await sender.send(event)
                    if ok:
                        log.info("  ✓ Delivered")
                    else:
                        log.warning("  ✗ Failed")

        except aiomqtt.MqttError as e:
            log.warning("MQTT disconnected: %s — reconnecting in %ds", e, retry_delay)
        except Exception as e:
            log.error("Unexpected error: %s — reconnecting in %ds", e, retry_delay)

        await asyncio.sleep(retry_delay)
        retry_delay = min(retry_delay * 2, 30)

    await sender.close()


if __name__ == "__main__":
    asyncio.run(run())
