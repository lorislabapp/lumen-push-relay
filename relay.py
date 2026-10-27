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
from src.review_contract import ReviewCorrelator
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

        Returns True if at least one URL accepted the payload — that
        matches the prior single-URL semantics so the main loop's
        ok/fail logging stays meaningful.
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
        headers["apns-collapse-id"] = hashlib.sha256(json.dumps([event.get("reviewId", event.get("review_id")), event.get("reviewSeverity", event.get("review_severity"))], separators=(",", ":")).encode()).hexdigest()
        success = True
        for token in self.device_tokens:
            url = f"{self.host}/3/device/{token}"
            try:
                resp = await self._client.post(url, json=payload, headers=headers)
                if resp.status_code == 200:
                    log.info("Push delivered to %s...%s", token[:8], token[-8:])
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
        self._last.pop(f"{camera}/{label}", None)

    def check_and_claim(self, camera: str, label: str) -> bool:
        key = f"{camera}/{label}"
        now = time.time()
        last = self._last.get(key, 0)
        if now - last < self.cooldown:
            return False
        self._last[key] = now
        return True


class ReviewTracker:
    """Deduplicate Review severity transitions with bounded memory."""

    def __init__(self, max_entries: int = 4096):
        self.max_entries = max_entries
        self._seen: dict[str, None] = {}

    def claim(self, review_id: str, severity: str) -> bool:
        key = f"{review_id}/{severity}"
        if key in self._seen:
            return False
        self._seen[key] = None
        while len(self._seen) > self.max_entries:
            self._seen.pop(next(iter(self._seen)))
        return True


def effective_mqtt_topic(config: dict) -> tuple[str, bool]:
    """Return (topic, is_review_topic), auto-migrating legacy defaults.

    Older compose snippets pinned ``frigate/events``. Keeping that value after
    an image update would continue to notify for raw tracked objects that never
    become a Frigate Review alert. Raw mode therefore requires an explicit
    ``ALLOW_RAW_EVENTS=true`` opt-in.
    """
    frigate = config.get("frigate", {})
    topic = str(frigate.get("topic") or "frigate/reviews").rstrip("/")
    raw_opt_in = frigate.get("allow_raw_events", False)
    allow_raw = raw_opt_in if isinstance(raw_opt_in, bool) else str(raw_opt_in).lower() in {
        "1", "true", "yes"
    }
    if topic.endswith("/events") and not allow_raw:
        topic = topic[: -len("/events")] + "/reviews"
    return topic, topic.endswith("/reviews")


def review_to_event(message: dict, allowed_severities: set[str]) -> tuple[dict | None, str | None]:
    """Convert a ``frigate/reviews`` change into a linked push event."""
    after = message.get("after")
    if not isinstance(after, dict):
        return None, "missing review after payload"

    severity = str(after.get("severity") or "")
    if severity not in allowed_severities:
        return None, f"review severity '{severity}' not enabled"

    before = message.get("before")
    before_severity = before.get("severity") if isinstance(before, dict) else None
    message_type = message.get("type")
    if message_type not in {"new", "update"}:
        return None, f"review message type '{message_type}' is not notifyable"
    if message_type == "update" and before_severity == severity:
        return None, f"review already at severity '{severity}'"

    data = after.get("data")
    if not isinstance(data, dict):
        return None, "missing review data"
    detections = data.get("detections") or []
    if not isinstance(detections, list) or not detections:
        return None, "review has no linked detection"
    event_id = next((value for value in detections if isinstance(value, str) and value), None)
    if not event_id:
        return None, "review has no valid detection id"

    objects = [value for value in (data.get("objects") or []) if isinstance(value, str) and value]
    zones = [value for value in (data.get("zones") or []) if isinstance(value, str)]
    start_time = after.get("start_time")
    return {
        "id": event_id,
        "review_id": after.get("id"),
        "review_severity": severity,
        "camera": after.get("camera", "unknown"),
        "label": objects[0] if objects else "activity",
        "labels": objects,
        "zones": zones,
        "current_zones": zones,
        "start_time": start_time,
        "detected_at": start_time,
        "source_schema": "frigate-review-v1",
    }, None


def build_apns_payload(event: dict) -> dict:
    """Build APNs content from an already-correlated Review envelope."""
    camera = event.get("camera", "unknown")
    label = event.get("label", "unknown")
    score = event.get("score", event.get("top_score", 0))
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
        "eventId": event.get("eventId", event.get("id", "")),
        "sourceSchema": event.get("sourceSchema", event.get("source_schema")),
        "reviewId": event.get("reviewId", event.get("review_id")),
        "reviewSeverity": event.get("reviewSeverity", event.get("review_severity")),
        "camera": camera,
        "label": label,
        "zones": zones,
        "top_score": score,
        "stationary": event.get("stationary"),
        "isEnrichment": event.get("isEnrichment", False),
        # Unix epoch seconds when Frigate first detected the event. The NSE uses
        # this to show "detected Xs ago" so the user can tell real-time pushes
        # apart from pushes that APNs held while the device was unreachable.
        "detected_at": event.get("detected_at", event.get("start_time")),
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
    event_labels = event.get("labels") or [label]
    allowed_labels = filters.get("labels", [])
    if allowed_labels:
        matching_labels = [value for value in event_labels if value in allowed_labels]
        if not matching_labels:
            return f"labels {event_labels!r} not in allowed list"
        label = matching_labels[0]
        event["label"] = label

    score = event.get("top_score")
    min_score = filters.get("min_score", 0)
    if type(min_score) not in (int, float) or not math.isfinite(min_score) or not 0 <= min_score <= 1:
        return "invalid confidence filter configuration"
    if type(score) not in (int, float) or not math.isfinite(score) or not 0 <= score <= 1:
        return "missing or invalid Frigate confidence"
    if score is not None and score < min_score:
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
        if cam_labels:
            matching_labels = [value for value in event_labels if value in cam_labels]
            if not matching_labels:
                return f"labels {event_labels!r} not allowed on camera '{camera}'"
            label = matching_labels[0]
            event["label"] = label
        cam_min = cam_cfg.get("min_score")
        if cam_min and score is not None and score < cam_min:
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


def _collect_push_urls_from_env() -> list[str]:
    """Return the list of worker URLs configured via env vars.

    Priority: PUSH_URLS (csv) overrides everything; otherwise PUSH_URL +
    PUSH_URL_2..PUSH_URL_N are concatenated in numeric order. Preserves
    order and drops duplicates / empties so the fan-out is predictable.
    """
    raw_csv = os.environ.get("PUSH_URLS", "").strip()
    if raw_csv:
        candidates = [u.strip() for u in raw_csv.split(",")]
    else:
        candidates = []
        if base := os.environ.get("PUSH_URL"):
            candidates.append(base.strip())
        # PUSH_URL_2, PUSH_URL_3, ... — accept a reasonable ceiling
        for n in range(2, 33):
            if extra := os.environ.get(f"PUSH_URL_{n}"):
                candidates.append(extra.strip())

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
                for value in os.environ.get("REVIEW_SEVERITIES", "alert").split(",")
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


async def run():
    """Main loop: subscribe to MQTT, filter events, send pushes."""
    config = load_config()

    frigate_cfg = config["frigate"]
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

    cooldown = CooldownTracker(cooldown_sec)
    configured = config.get("filters", {}).get("review_severities", ["alert"])
    if isinstance(configured, str): configured = configured.split(",")
    correlator = ReviewCorrelator(
        max_age_seconds=float(config.get("filters", {}).get("max_event_age_seconds", 120)),
        allowed_severities=frozenset(str(x).strip() for x in configured),
        state_path=os.environ.get("LUMEN_REVIEW_STATE_PATH", "/config/review-delivery-state.json"),
        event_filter=lambda event: should_notify(event, config) is None,
    )
    # Preserve custom MQTT prefixes; raw objects never independently authorize a push.
    topic = str(frigate_cfg.get("topic") or "frigate/reviews").rstrip("/")
    prefix = topic.rsplit("/", 1)[0] if topic.endswith(("/events", "/reviews")) else "frigate"
    events_topic = os.environ.get("LUMEN_EVENT_TOPIC", prefix + "/events")
    review_topic = os.environ.get("LUMEN_REVIEW_TOPIC", prefix + "/reviews")
    identity_topic = os.environ.get("LUMEN_IDENTITY_TOPIC", prefix + "/tracked_object_update")

    async def deliver(envelope):
        enrichment = envelope.get("isEnrichment", False)
        camera, label = envelope["camera"], envelope["label"]
        if not enrichment and not cooldown.check_and_claim(camera, label):
            correlator.complete_delivery(envelope, terminal=True, notified=False)
            return
        message = get_zone_message({"zones": envelope.get("zones", [])}, config)
        if message: envelope = {**envelope, **message}
        try:
            accepted = await sender.send(envelope)
        except Exception:
            accepted = False
        correlator.complete_delivery(envelope, terminal=accepted)
        if not accepted and not enrichment: cooldown.release(camera, label)
        log.info("Review delivery %s", "accepted" if accepted else "retryable failure")

    retry_delay = 1
    try:
        while True:
            try:
                options = {"hostname": frigate_cfg["mqtt_host"], "port": frigate_cfg["mqtt_port"]}
                if frigate_cfg.get("mqtt_user"): options["username"] = frigate_cfg["mqtt_user"]
                if frigate_cfg.get("mqtt_password"): options["password"] = frigate_cfg["mqtt_password"]
                async with aiomqtt.Client(**options) as mqtt:
                    for name in dict.fromkeys([events_topic, review_topic, identity_topic]):
                        await mqtt.subscribe(name)
                    log.info("Connected to MQTT; Review correlation active")
                    retry_delay = 1
                    async for message in mqtt.messages:
                        try:
                            value = json.loads(message.payload)
                            name = str(message.topic)
                            if name == events_topic: envelopes = correlator.ingest_event_message(value)
                            elif name == review_topic: envelopes = correlator.ingest_review_message(value)
                            elif name == identity_topic: envelopes = correlator.ingest_identity_update(value)
                            else: continue
                            for envelope in envelopes: await deliver(envelope)
                        except (ValueError, TypeError, KeyError):
                            log.warning("Rejected malformed MQTT message")
            except aiomqtt.MqttError:
                log.warning("MQTT disconnected; retrying in %ds", retry_delay)
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 60)
    finally:
        await sender.close()


if __name__ == "__main__":
    asyncio.run(run())
