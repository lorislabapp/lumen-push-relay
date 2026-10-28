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
from src.review_contract import ReviewCorrelator, last_activity
import os
import time
from dataclasses import dataclass, field
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

RELAY_VERSION = "1.2.2"

# Worker outcomes that mean "the Worker deliberately did not push" — terminal,
# not worth retrying. rate_limited and failed are retryable.
_WORKER_POLICY_OUTCOMES = frozenset({"duplicate", "filtered", "cooldown"})
WORKER_RATE_LIMIT_RETRY_SECONDS = 6.0


@dataclass
class DeliveryResult:
    """Outcome of one Review delivery across every configured URL.

    accepted: terminal — record delivery state, do not retry.
    notified: at least one device was actually pushed.
    """
    accepted: bool
    notified: bool
    outcomes: list = field(default_factory=list)

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

    def __init__(self, push_urls: list[str], *, client: httpx.AsyncClient | None = None, sleep=None):
        if not push_urls:
            raise ValueError("WorkerSender requires at least one URL")
        self.push_urls = list(push_urls)
        self._client = client or httpx.AsyncClient(timeout=10)
        self._sleep = sleep or asyncio.sleep

    async def _post(self, url: str, payload: dict) -> tuple[str, list]:
        """One POST. Returns (status, outcomes) with status one of
        "sent", "suppressed", "rate_limited", "failed"."""
        try:
            resp = await self._client.post(
                url,
                json=payload,
                headers={"Content-Type": "application/json"},
            )
        except Exception as e:
            log.error("Worker request failed for %s: %s", _mask_url(url), type(e).__name__)
            return "failed", []
        if resp.status_code != 200:
            log.error("Worker error %d for %s: %s", resp.status_code, _mask_url(url), resp.text[:200])
            return "failed", []
        return classify_worker_response(resp.text, _mask_url(url))

    async def _send_one(self, url: str, payload: dict) -> tuple[str, list]:
        status, outcomes = await self._post(url, payload)
        if status == "rate_limited":
            log.info("Worker rate-limited %s; retrying once in %.0fs", _mask_url(url), WORKER_RATE_LIMIT_RETRY_SECONDS)
            await self._sleep(WORKER_RATE_LIMIT_RETRY_SECONDS)
            status, outcomes = await self._post(url, payload)
        return status, outcomes

    async def send(self, event: dict) -> DeliveryResult:
        """POST one canonical Review envelope to every configured Worker URL.

        Terminal (accepted) only when no URL needs a retry. A URL whose Worker
        deliberately suppressed the push (duplicate/filtered/cooldown) counts
        as terminal but not as notified.
        """
        results = await asyncio.gather(
            *(self._send_one(url, event) for url in self.push_urls),
            return_exceptions=False,
        )
        statuses = [status for status, _ in results]
        retryable = sum(1 for status in statuses if status in {"failed", "rate_limited"})
        if retryable:
            log.warning("Fan-out partial: %d/%d URLs need a retry", retryable, len(self.push_urls))
        return DeliveryResult(
            accepted=retryable == 0,
            notified=any(status == "sent" for status in statuses),
            outcomes=[outcome for _, outs in results for outcome in outs],
        )

    async def close(self):
        await self._client.aclose()


def classify_worker_response(body: str, masked_url: str = "") -> tuple[str, list]:
    """Interpret a 200 response from the lumen-push Worker.

    Newer Workers answer JSON {"ok", "delivered", "suppressed", "outcomes"};
    older ones answer the plain text "OK", which keeps meaning "sent".
    """
    text = (body or "").strip()
    if not text.startswith("{"):
        return "sent", []
    try:
        data = json.loads(text)
    except ValueError:
        return "sent", []
    if not isinstance(data, dict):
        return "sent", []
    raw = data.get("outcomes")
    outcomes = []
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, dict) and isinstance(item.get("outcome"), str):
            reason = item.get("reason")
            outcomes.append({
                "outcome": item["outcome"][:32],
                "reason": reason[:120] if isinstance(reason, str) else "",
            })
    delivered = data.get("delivered")
    suppressed = data.get("suppressed")
    log.info(
        "Worker %s: ok=%s delivered=%s suppressed=%s outcomes=[%s]",
        masked_url, data.get("ok"), delivered, suppressed,
        ", ".join(o["outcome"] + (f"({o['reason']})" if o["reason"] else "") for o in outcomes[:16]),
    )
    if data.get("ok") is False:
        return "failed", outcomes
    if type(delivered) is not int:
        # JSON without a delivered count: trust ok, as with plain "OK".
        return "sent", outcomes
    if delivered > 0:
        return "sent", outcomes
    kinds = {o["outcome"] for o in outcomes}
    if "rate_limited" in kinds:
        return "rate_limited", outcomes
    if kinds and kinds <= _WORKER_POLICY_OUTCOMES:
        return "suppressed", outcomes
    return "failed", outcomes


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

    camera_display = event.get("camera_display_name") or camera.replace("_", " ").title()

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
    # Age is measured from the object's latest Frigate activity (frame_time /
    # end_time), so the "end" message carrying the final score is not stale.
    max_age = filters.get("max_event_age_seconds", 120)
    start_time = event.get("start_time")
    if max_age and start_time:
        age = time.time() - (last_activity(event) or float(start_time))
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


def suppression_code(reason: str | None) -> str | None:
    """Map a should_notify() reason to a stable log code."""
    if reason is None:
        return None
    if reason.startswith("stale event"):
        return "expired"
    if reason.startswith("score "):
        return "below_min_score"
    if reason.startswith("labels "):
        return "label_filtered"
    if reason == "stationary":
        return "stationary"
    if reason.startswith("camera ") and "schedule" in reason:
        return "schedule"
    if reason.startswith("camera "):
        return "camera_filtered"
    if reason.startswith("not in required zones"):
        return "zone_filtered"
    if "confidence" in reason:
        return "invalid_score"
    return "filtered"


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


def _review_severities_from_env() -> str:
    """REVIEW_SEVERITIES, with FILTER_REVIEW_SEVERITIES as deprecated alias."""
    current = os.environ.get("REVIEW_SEVERITIES", "").strip()
    legacy = os.environ.get("FILTER_REVIEW_SEVERITIES", "").strip()
    if legacy and current:
        log.warning("FILTER_REVIEW_SEVERITIES is deprecated and ignored because REVIEW_SEVERITIES is set")
    elif legacy:
        log.warning("FILTER_REVIEW_SEVERITIES is deprecated; rename it to REVIEW_SEVERITIES")
        return legacy
    return current or "alert"


class CameraNameResolver:
    """Opt-in camera id → Frigate friendly_name map, read from /api/config.

    Refreshed at start and every refresh_seconds. Any failure keeps the last
    good map (or none); it never blocks or fails a delivery.
    """

    MAX_LEN = 64

    def __init__(self, base_url: str, *, user: str | None = None, password: str | None = None,
                 token: str | None = None, tls_verify: bool = True, refresh_seconds: float = 600,
                 client: httpx.AsyncClient | None = None):
        self.base_url = base_url.rstrip("/")
        self.user, self.password, self.token = user, password, token
        self.refresh_seconds = refresh_seconds
        self._client = client or httpx.AsyncClient(timeout=10, verify=tls_verify)
        self.names: dict[str, str] = {}

    @staticmethod
    def _clean(value) -> str | None:
        if not isinstance(value, str):
            return None
        text = "".join(ch for ch in value if ch.isprintable()).strip()
        return text[: CameraNameResolver.MAX_LEN] or None

    async def refresh(self) -> bool:
        try:
            headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
            if self.user and self.password and not self.token:
                login = await self._client.post(f"{self.base_url}/api/login",
                                                json={"user": self.user, "password": self.password})
                if login.status_code not in (200, 204):
                    log.warning("Frigate login for camera names failed (HTTP %d)", login.status_code)
                    return False
            resp = await self._client.get(f"{self.base_url}/api/config", headers=headers)
            if resp.status_code != 200:
                log.warning("Frigate /api/config for camera names failed (HTTP %d)", resp.status_code)
                return False
            cameras = resp.json().get("cameras")
            if not isinstance(cameras, dict):
                return False
            names = {}
            for camera_id, camera_cfg in cameras.items():
                name = self._clean(camera_cfg.get("friendly_name")) if isinstance(camera_cfg, dict) else None
                if isinstance(camera_id, str) and name:
                    names[camera_id] = name
            self.names = names
            log.info("Camera display names loaded for %d camera(s)", len(names))
            return True
        except Exception as e:
            log.warning("Frigate camera names unavailable: %s", type(e).__name__)
            return False

    def display_name(self, camera: str) -> str | None:
        return self.names.get(camera)

    async def run_forever(self) -> None:
        while True:
            await self.refresh()
            await asyncio.sleep(self.refresh_seconds)

    async def close(self) -> None:
        await self._client.aclose()


def camera_resolver_from_config(frigate_cfg: dict) -> "CameraNameResolver | None":
    url = frigate_cfg.get("url")
    if not isinstance(url, str) or not url.strip():
        return None
    if not url.startswith(("http://", "https://")):
        log.warning("FRIGATE_URL must start with http:// or https://; camera names disabled")
        return None
    return CameraNameResolver(
        url.strip(),
        user=frigate_cfg.get("api_user"),
        password=frigate_cfg.get("api_password"),
        token=frigate_cfg.get("api_token"),
        tls_verify=frigate_cfg.get("tls_verify", True) is not False,
    )


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
                for value in _review_severities_from_env().split(",")
                if value.strip()
            ],
        },
    }
    frigate_url = os.environ.get("FRIGATE_URL", "").strip()
    if frigate_url:
        config["frigate"]["url"] = frigate_url
        for env_name, key in (("FRIGATE_USER", "api_user"), ("FRIGATE_PASSWORD", "api_password"),
                              ("FRIGATE_TOKEN", "api_token")):
            if os.environ.get(env_name):
                config["frigate"][key] = os.environ[env_name]
        verify = os.environ.get("FRIGATE_TLS_VERIFY", "true").strip().lower()
        config["frigate"]["tls_verify"] = verify not in {"0", "false", "no"}

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
    log.info("lumen-push-relay %s starting", RELAY_VERSION)

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
        event_rejection=lambda event: suppression_code(should_notify(event, config)),
    )
    camera_names = camera_resolver_from_config(frigate_cfg)
    names_task = asyncio.create_task(camera_names.run_forever()) if camera_names else None
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
        display = camera_names.display_name(camera) if camera_names else None
        if display: envelope = {**envelope, "camera_display_name": display[:64]}
        try:
            result = await sender.send(envelope)
        except Exception:
            result = False
        if isinstance(result, DeliveryResult):
            accepted, notified = result.accepted, result.notified
        else:  # DirectAPNsSender / legacy senders return a bool
            accepted = notified = bool(result)
        correlator.complete_delivery(envelope, terminal=accepted, notified=notified)
        if not notified and not enrichment: cooldown.release(camera, label)
        state = "sent" if notified else ("suppressed by Worker" if accepted else "retryable failure")
        log.info("Review %s delivery %s", str(envelope.get("reviewId", ""))[:64], state)

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
        if names_task:
            names_task.cancel()
        if camera_names:
            await camera_names.close()
        await sender.close()


if __name__ == "__main__":
    asyncio.run(run())
