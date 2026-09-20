#!/usr/bin/env python3
"""Frigate Push Relay — sends push notifications for Frigate review alerts.

Supports two modes:
  - "worker" mode: POSTs events to a Cloudflare Worker URL (recommended for most users)
  - "direct" mode: Signs APNs JWTs and sends directly to Apple (requires APNs key)
"""

import asyncio
import json
import logging
import math
import hashlib
import os
import time
from datetime import datetime
from pathlib import Path

import aiomqtt
import httpx
import yaml
from src.review_contract import ReviewCorrelator

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
    """Sends events to one or more Cloudflare Worker webhook URLs.

    Accepts a list of URLs so a single Frigate event fans out to every
    registered device. Typical use: one URL per Lumen install
    (iPhone + iPad + Mac + Apple Watch + Vision Pro). Filters and cooldown
    run once *before* fan-out, so each event produces at most one push per
    device regardless of how many URLs are configured.
    """

    def __init__(self, push_urls: list[str]):
        if not push_urls:
            raise ValueError("WorkerSender requires at least one URL")
        self.push_urls = list(push_urls)
        self._client = httpx.AsyncClient(timeout=10)

    async def _send_one(self, url: str, payload: dict) -> bool:
        try:
            resp = await self._client.post(
                url,
                json=payload,
                headers={"Content-Type": "application/json"},
            )
            if resp.status_code == 200:
                return True
            log.error("Worker error %d for %s: %s", resp.status_code, _mask_url(url), resp.text)
            return False
        except Exception as e:
            log.error("Worker request failed for %s: %s", _mask_url(url), e)
            return False

    async def send(self, event: dict) -> bool:
        """POST one canonical Review envelope to every configured Worker URL.

        Returns True only when every destination accepted the envelope.
        Failed destinations must remain retryable.
        """
        results = await asyncio.gather(
            *(self._send_one(url, event) for url in self.push_urls),
            return_exceptions=False,
        )
        delivered = sum(1 for ok in results if ok)
        if delivered < len(self.push_urls):
            log.warning("Fan-out partial: %d/%d URLs delivered", delivered, len(self.push_urls))
        return delivered == len(self.push_urls)

    async def close(self):
        await self._client.aclose()


def _mask_url(url: str) -> str:
    """Redact the secret+token portion of a webhook URL for logs."""
    # Shape: https://host/v1/notify/{secret}/{token} — keep host + prefix + last 6
    try:
        parts = url.split("/")
        if len(parts) >= 7:
            tail = parts[-1]
            redacted_tail = "…" + tail[-6:] if len(tail) > 6 else tail
            return "/".join(parts[:-2]) + "/…/" + redacted_tail
    except Exception:
        pass
    return url[:40] + "…"


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
        self._accepted: dict[str, None] = {}
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
            "apns-priority": "5" if event.get("isEnrichment") else "10",
            "apns-collapse-id": hashlib.sha256(json.dumps([event["reviewId"], event["reviewSeverity"]]).encode()).hexdigest(),
            "apns-expiration": str(expires_at),
        }
        success = True
        for token in self.device_tokens:
            receipt = json.dumps([token, event["reviewId"], event["reviewSeverity"], bool(event.get("isEnrichment"))])
            if receipt in self._accepted:
                continue
            url = f"{self.host}/3/device/{token}"
            try:
                resp = await self._client.post(url, json=payload, headers=headers)
                if resp.status_code == 200:
                    self._accepted[receipt] = None
                    while len(self._accepted) > 4096:
                        self._accepted.pop(next(iter(self._accepted)))
                    log.info("Push accepted by APNs")
                else:
                    success = False
                    log.error("APNs error %d for %s...%s: %s", resp.status_code, token[:8], token[-8:], resp.text)
            except Exception as e:
                success = False
                log.error("APNs request failed for %s...%s: %s", token[:8], token[-8:], e)
        return success

    async def close(self):
        await self._client.aclose()


class CooldownTracker:
    """Per camera+label cooldown to avoid notification spam."""

    def __init__(self, cooldown_seconds: int):
        self.cooldown = cooldown_seconds
        self._last: dict[str, float] = {}

    def release(self, camera: str, label: str) -> None:
        """A failed transport must not consume the next retry's cooldown."""
        self._last.pop(f"{camera}/{label}", None)

    def check_and_claim(self, camera: str, label: str) -> bool:
        key = f"{camera}/{label}"
        now = time.time()
        last = self._last.get(key, 0)
        if now - last < self.cooldown:
            return False
        self._last[key] = now
        return True




def effective_mqtt_topic(config: dict) -> tuple[str, bool]:
    """Keep legacy/custom prefixes, but never grant raw objects push authority."""
    topic = str(config.get("frigate", {}).get("topic") or "frigate/reviews").rstrip("/")
    if topic.endswith("/events"):
        topic = topic[:-len("/events")] + "/reviews"
    if not topic.endswith("/reviews"):
        raise ValueError("MQTT_TOPIC must end in /reviews or /events")
    return topic, True




def build_apns_payload(event: dict) -> dict:
    """Build APNs content from an already-correlated Review envelope."""
    camera = event.get("camera", "unknown")
    label = event.get("label", "unknown")
    score = event.get("score", 0)
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

    payload = {
        "aps": {
            "alert": {
                "title": title,
                "subtitle": camera_display,
                "body": body,
            },
            "sound": None if event.get("isEnrichment") else "default",
            "interruption-level": "passive" if event.get("isEnrichment") else "time-sensitive",
            "category": "FRIGATE_EVENT",
            "thread-id": camera,
            "mutable-content": 1,
        },
        "eventId": event.get("eventId", ""),
        "sourceSchema": event.get("sourceSchema"),
        "reviewId": event.get("reviewId"),
        "reviewSeverity": event.get("reviewSeverity"),
        "camera": camera,
        "label": label,
        "zones": zones,
        "top_score": score,
        "stationary": event.get("stationary"),
        "isEnrichment": event.get("isEnrichment", False),
        # Unix epoch seconds when Frigate first detected the event. The NSE uses
        # this to show "detected Xs ago" so the user can tell real-time pushes
        # apart from pushes that APNs held while the device was unreachable.
        "detected_at": event.get("detected_at"),
        "source": event.get("source", "mqtt-relay"),
    }
    if event.get("isEnrichment"):
        payload["aps"].pop("sound", None)
    return payload


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

    # Drop stale events (MQTT replay, relay backlog, broker queue). Forwarding
    # these produces "fake-fresh" pushes on the device — user sees a banner now
    # for something that happened hours ago. Default 120s, configurable via
    # FILTER_MAX_EVENT_AGE / filters.max_event_age_seconds (0 disables).
    max_age = filters.get("max_event_age_seconds", 120)
    start_time = event.get("start_time")
    if max_age and start_time:
        age = time.time() - float(start_time)
        if age > max_age:
            return f"stale event (age={age:.0f}s > {max_age}s)"

    label = event.get("label", "")
    allowed_labels = filters.get("labels", [])
    if allowed_labels and label not in allowed_labels:
        return f"label '{label}' not in allowed list"

    score = event.get("top_score", 0)
    min_score = filters.get("min_score", 0)
    if type(min_score) not in (int, float) or not math.isfinite(min_score) or not 0 <= min_score <= 1:
        return "invalid confidence filter configuration"
    if type(score) not in (int, float) or not math.isfinite(score) or not 0 <= score <= 1:
        return "missing or invalid Frigate confidence"
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


def _clean_env_url(raw: str) -> str:
    """Normalise a URL pulled from an env var.

    Docker Compose's list-form `environment:` keeps surrounding quotes as
    part of the value (e.g. `- PUSH_URL="https://x/y "` yields the literal
    string `"https://x/y "`, quotes and trailing space included). urllib
    then rejects it with "Request URL is missing an 'http://' or 'https://'
    protocol." Strip whitespace, then any matching surrounding quotes, then
    whitespace again so quoted/padded values still work.
    """
    s = raw.strip()
    while len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        s = s[1:-1].strip()
    return s


def _collect_push_urls_from_env() -> list[str]:
    """Return the list of worker URLs configured via env vars.

    Priority: PUSH_URLS (csv) overrides everything; otherwise PUSH_URL +
    PUSH_URL_2..PUSH_URL_N are concatenated in numeric order. Preserves
    order and drops duplicates / empties so the fan-out is predictable.
    """
    raw_csv = _clean_env_url(os.environ.get("PUSH_URLS", ""))
    if raw_csv:
        candidates = [_clean_env_url(u) for u in raw_csv.split(",")]
    else:
        candidates = []
        if base := os.environ.get("PUSH_URL"):
            candidates.append(_clean_env_url(base))
        # PUSH_URL_2, PUSH_URL_3, ... — accept a reasonable ceiling
        for n in range(2, 33):
            if extra := os.environ.get(f"PUSH_URL_{n}"):
                candidates.append(_clean_env_url(extra))

    seen = set()
    ordered: list[str] = []
    for u in candidates:
        if u and u not in seen:
            seen.add(u)
            ordered.append(u)
    return ordered


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
            "topic": os.environ.get("MQTT_TOPIC", "frigate/reviews"),
            "allow_raw_events": os.environ.get("ALLOW_RAW_EVENTS", "").lower()
            in {"1", "true", "yes"},
        },
        "filters": {
            "labels": os.environ.get("FILTER_LABELS", "person,car,dog,cat,package").split(","),
            "min_score": float(os.environ.get("FILTER_MIN_SCORE", "0.6")),
            "cooldown_seconds": int(os.environ.get("COOLDOWN_SECONDS", "120")),
            "max_event_age_seconds": int(os.environ.get("FILTER_MAX_EVENT_AGE", "120")),
            "review_severities": [
                value.strip()
                for value in os.environ.get("FILTER_REVIEW_SEVERITIES", os.environ.get("REVIEW_SEVERITIES", "alert")).split(",")
                if value.strip()
            ],
        },
    }

    # Worker mode (recommended).
    #
    # Accepts:
    #   PUSH_URL            (single URL — keeps pre-1.2 behaviour)
    #   PUSH_URL_2, ..._N   (additional URLs — one per device, e.g. Watch)
    #   PUSH_URLS           (comma-separated list; takes precedence if set)
    push_urls = _collect_push_urls_from_env()
    if push_urls:
        config["mode"] = "worker"
        config["worker"] = {"push_urls": push_urls}
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


class NotificationPipeline:
    """Correlate Review authority with real object confidence before any send."""

    def __init__(self, config: dict, sender):
        self.config = config
        self.sender = sender
        review_topic, _ = effective_mqtt_topic(config)
        prefix = review_topic[:-len("/reviews")]
        self.topics = (review_topic, prefix + "/events", prefix + "/tracked_object_update")
        filters = config.get("filters", {})
        severities = filters.get("review_severities", ["alert"])
        if isinstance(severities, str):
            severities = [v.strip() for v in severities.split(",") if v.strip()]
        self.cooldown = CooldownTracker(filters.get("cooldown_seconds", 120))
        self.correlator = ReviewCorrelator(
            state_path=os.environ.get("LUMEN_REVIEW_STATE_PATH", "/config/review-delivery-state.json"),
            allowed_severities=frozenset(severities),
            event_filter=lambda event: should_notify(event, config) is None,
        )

    async def handle(self, topic: str, message: dict):
        if topic == self.topics[0]:
            envelopes = self.correlator.ingest_review_message(message)
        elif topic == self.topics[1]:
            envelopes = self.correlator.ingest_event_message(message)
        elif topic == self.topics[2]:
            envelopes = self.correlator.ingest_identity_update(message)
        else:
            return
        for envelope in envelopes:
            await self.deliver(envelope)

    async def deliver(self, envelope: dict):
        event = {"id": envelope["eventId"], "camera": envelope["camera"],
                 "label": envelope["label"], "top_score": envelope["score"],
                 "start_time": envelope["detected_at"], "zones": envelope.get("zones", []),
                 "stationary": envelope.get("stationary", False)}
        reason = should_notify(event, self.config)
        camera, label = event["camera"], event["label"]
        enrichment = bool(envelope.get("isEnrichment"))
        if reason or (not enrichment and not self.cooldown.check_and_claim(camera, label)):
            self.correlator.complete_delivery(envelope, terminal=True, notified=False)
            return
        zone_message = get_zone_message(event, self.config)
        if zone_message:
            envelope = {**envelope, **zone_message}
        try:
            accepted = await self.sender.send(envelope)
        except Exception as error:
            log.warning("Review transport failed (%s)", type(error).__name__)
            accepted = False
        self.correlator.complete_delivery(envelope, terminal=accepted)
        if not accepted and not enrichment:
            self.cooldown.release(camera, label)
        log.info("Review %s: %s (%.0f%%)", envelope["reviewId"],
                 "accepted" if accepted else "transport failed; retry available", envelope["score"] * 100)


async def run():
    """Main loop: subscribe to MQTT, filter events, send pushes."""
    config = load_config()

    frigate_cfg = config["frigate"]
    mqtt_topic, is_review_topic = effective_mqtt_topic(config)
    cooldown_sec = config.get("filters", {}).get("cooldown_seconds", 120)

    # Determine mode. Config files may specify either `worker.push_urls`
    # (list, new) or the legacy `worker.push_url` (single). Env vars flow
    # through load_config() and always produce the list form.
    mode = config.get("mode", "direct")
    worker_cfg = config.get("worker", {})
    push_urls = worker_cfg.get("push_urls") or (
        [worker_cfg["push_url"]] if worker_cfg.get("push_url") else []
    )

    if push_urls or mode == "worker":
        if not push_urls:
            log.error("Worker mode requires PUSH_URL (or PUSH_URLS / PUSH_URL_2..N) — exiting")
            return
        sender = WorkerSender(push_urls)
        log.info(
            "Mode: WORKER → %d URL%s [%s]",
            len(push_urls),
            "" if len(push_urls) == 1 else "s",
            ", ".join(_mask_url(u) for u in push_urls),
        )
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

    pipeline = NotificationPipeline(config, sender)
    review_topic, event_topic, identity_topic = pipeline.topics

    log.info(
        "Connecting to MQTT %s:%d topic=%s",
        frigate_cfg["mqtt_host"],
        frigate_cfg["mqtt_port"],
        mqtt_topic,
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
                for topic in pipeline.topics:
                    await mqtt.subscribe(topic)
                log.info(
                    "Connected to MQTT, listening for %s...",
                    "Review alerts" if is_review_topic else "raw tracked objects",
                )
                retry_delay = 1

                async for message in mqtt.messages:
                    try:
                        data = json.loads(message.payload)
                    except (json.JSONDecodeError, TypeError):
                        continue

                    await pipeline.handle(str(message.topic), data)

        except aiomqtt.MqttError as e:
            log.warning("MQTT disconnected: %s — reconnecting in %ds", e, retry_delay)
        except Exception as e:
            log.error("Unexpected error: %s — reconnecting in %ds", e, retry_delay)

        await asyncio.sleep(retry_delay)
        retry_delay = min(retry_delay * 2, 30)

    await sender.close()


if __name__ == "__main__":
    asyncio.run(run())
