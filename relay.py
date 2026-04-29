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

from src.auto_call import ConfigStore, CooldownStore, TriggerEngine, VoIPDispatcher
from src.auto_call.apns_jwt import APNsJWTSigner
from src.auto_call.buttons import ButtonEngine, MatchEvaluator
from src.auto_call.discovery import DiscoveryCache
from src.auto_call.mqtt import SubscriptionManager, TopicRouter
from src.auto_call.sync_endpoint import start_sync_server

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
        """POST a Frigate event payload to every configured Worker URL.

        Returns True if at least one URL accepted the payload — that
        matches the prior single-URL semantics so the main loop's
        ok/fail logging stays meaningful.
        """
        # The Worker expects the same format as Frigate MQTT: {"type":"new","after":{...}}
        payload = {"type": "new", "before": None, "after": event}
        results = await asyncio.gather(
            *(self._send_one(url, payload) for url in self.push_urls),
            return_exceptions=False,
        )
        delivered = sum(1 for ok in results if ok)
        if delivered < len(self.push_urls):
            log.warning("Fan-out partial: %d/%d URLs delivered", delivered, len(self.push_urls))
        return delivered > 0

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

    def __init__(self, key_file: str, key_id: str, team_id: str, bundle_id: str, environment: str, device_tokens: list[str], signer: "APNsJWTSigner | None" = None):
        # JWT signing is delegated to APNsJWTSigner (shared with VoIPDispatcher).
        # Accept an optional pre-built signer so the auto-call subsystem and the
        # main alert path use the same .p8/key_id/team_id without loading the
        # file twice.
        self.key_id = key_id
        self.team_id = team_id
        self.bundle_id = bundle_id
        self.host = APNS_HOST[environment]
        if signer is None:
            private_key = Path(key_file).read_text()
            signer = APNsJWTSigner(team_id=team_id, key_id=key_id, private_key_pem=private_key)
        self._signer = signer
        self.device_tokens = device_tokens
        self._client = httpx.AsyncClient(http2=True, timeout=10)

    def _get_token(self) -> str:
        # Backward-compatible wrapper around the shared signer. Kept so
        # external callers (or older tests) that referenced this method
        # continue to work; the signer caches internally.
        return self._signer.token()

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
        # Unix epoch seconds when Frigate first detected the event. The NSE uses
        # this to show "detected Xs ago" so the user can tell real-time pushes
        # apart from pushes that APNs held while the device was unreachable.
        "detected_at": event.get("start_time"),
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
            "topic": os.environ.get("MQTT_TOPIC", "frigate/events"),
        },
        "filters": {
            "labels": os.environ.get("FILTER_LABELS", "person,car,dog,cat,package").split(","),
            "min_score": float(os.environ.get("FILTER_MIN_SCORE", "0.6")),
            "cooldown_seconds": int(os.environ.get("COOLDOWN_SECONDS", "120")),
            "max_event_age_seconds": int(os.environ.get("FILTER_MAX_EVENT_AGE", "120")),
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
    ha_discovery_prefix = os.environ.get("LUMEN_HA_DISCOVERY_PREFIX", "homeassistant").strip("/")

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

    # ---- Auto-call (Doorbell Call Mode) -----------------------------------
    # Phase 2.D: optional VoIP push pipeline. Always-additive — never blocks
    # the regular alert path. Disabled if APNs creds aren't available, since
    # VoIP must go direct to api.push.apple.com (no Worker proxy).
    engine: TriggerEngine | None = None
    voip_dispatcher: VoIPDispatcher | None = None
    sync_runner = None  # aiohttp AppRunner
    config_store: ConfigStore | None = None
    cooldown_store: CooldownStore | None = None
    discovery_cache: DiscoveryCache | None = None
    button_engine: ButtonEngine | None = None
    subs_reconcile_queue: asyncio.Queue | None = None

    autocall_enabled = os.environ.get("LUMEN_AUTOCALL_ENABLED", "true").lower() not in ("0", "false", "no")
    if autocall_enabled:
        try:
            # Reuse the DirectAPNsSender's signer if we built one above; else
            # try to construct one from env. This is what makes auto-call work
            # in worker mode too — the worker doesn't sign, but we do for VoIP.
            signer: APNsJWTSigner | None = None
            if isinstance(sender, DirectAPNsSender):
                signer = sender._signer
            else:
                try:
                    signer = APNsJWTSigner.from_env()
                except (KeyError, OSError) as e:
                    log.warning(
                        "auto_call: cannot load APNs signer (%s) — VoIP pushes disabled. "
                        "Set APNS_TEAM_ID / APNS_KEY_ID / APNS_KEY_FILE to enable.", e,
                    )
                    signer = None

            if signer is not None:
                voip_dispatcher = VoIPDispatcher(jwt_provider=signer.token)
                config_store = ConfigStore()
                cooldown_store = CooldownStore()
                engine = TriggerEngine(config=config_store, dispatcher=voip_dispatcher, cooldown=cooldown_store)

                # Wire the button + discovery pipeline before starting the sync server,
                # so handle_sync can access discovery_cache and subs_reconcile_queue
                # from the very first request.
                discovery_cache = DiscoveryCache()
                button_engine = ButtonEngine(
                    config_store=config_store,
                    dispatcher=voip_dispatcher,
                    cooldown=cooldown_store,
                )
                subs_reconcile_queue = asyncio.Queue(maxsize=8)

                sync_host = os.environ.get("LUMEN_AUTOCALL_SYNC_HOST", "0.0.0.0")
                sync_port = int(os.environ.get("LUMEN_AUTOCALL_SYNC_PORT", "8765"))
                sync_runner = await start_sync_server(
                    host=sync_host, port=sync_port,
                    discovery_cache=discovery_cache,
                    subs_reconcile_queue=subs_reconcile_queue,
                )
                log.info("auto_call: engine ready, sync REST endpoint on %s:%d", sync_host, sync_port)
                log.info("auto_call: button engine + HA discovery wired (prefix=%s/)", ha_discovery_prefix)
        except Exception as e:
            # Auto-call must NEVER break the relay — log loudly and carry on.
            log.error("auto_call: failed to initialise (%s) — continuing without VoIP pipeline", e)
            engine = None
            voip_dispatcher = None
            sync_runner = None
            config_store = None
            cooldown_store = None
            discovery_cache = None
            button_engine = None
            subs_reconcile_queue = None
    else:
        log.info("auto_call: disabled via LUMEN_AUTOCALL_ENABLED")

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
                if discovery_cache is not None:
                    await mqtt.subscribe(f"{ha_discovery_prefix}/+/+/config")

                # Fresh per MQTT connection — the broker session is gone on reconnect,
                # so subscription state must restart from empty.
                subs_manager: SubscriptionManager | None = None
                if button_engine is not None and config_store is not None:
                    subs_manager = SubscriptionManager(mqtt)
                    await subs_manager.reconcile(config_store.all_button_topics())

                async def _frigate_handler(topic: str, payload: bytes):
                    # Defensive guard: the message loop short-circuits Frigate-topic
                    # messages BEFORE invoking the router, so this handler must never
                    # run. If it does, our routing assumption is broken — fail loud
                    # so it surfaces in logs (TopicRouter.route catches and logs).
                    raise AssertionError("frigate path must run inline, not via TopicRouter")

                router: TopicRouter | None = None
                if button_engine is not None and discovery_cache is not None:
                    router = TopicRouter(
                        frigate_topic=frigate_cfg["topic"],
                        ha_prefix=ha_discovery_prefix,
                        frigate_handler=_frigate_handler,
                        discovery_cache=discovery_cache,
                        button_engine=button_engine,
                    )

                log.info("Connected to MQTT, listening for events...")
                retry_delay = 1

                async for message in mqtt.messages:
                    topic_str = str(message.topic)
                    payload_bytes = message.payload if isinstance(message.payload, (bytes, bytearray)) else bytes(message.payload or b"")

                    # Drain any pending reconcile signals.
                    if subs_manager is not None and config_store is not None and subs_reconcile_queue is not None:
                        drained = False
                        try:
                            while True:
                                subs_reconcile_queue.get_nowait()
                                drained = True
                        except asyncio.QueueEmpty:
                            pass
                        if drained:
                            config_store.reload()
                            await subs_manager.reconcile(config_store.all_button_topics())

                    # Non-Frigate messages: route via TopicRouter (HA discovery + buttons).
                    if topic_str != frigate_cfg["topic"]:
                        if router is not None:
                            await router.route(topic_str, payload_bytes)
                        continue

                    # Frigate path (existing — UNCHANGED below).
                    try:
                        data = json.loads(payload_bytes)
                    except (json.JSONDecodeError, TypeError):
                        continue

                    if data.get("type") != "new":
                        continue

                    event = data.get("after") or data.get("before")
                    if not event:
                        continue

                    # Auto-call (Doorbell Call Mode) runs on every parsed event,
                    # independently of the alert pipeline. Its own filters
                    # (min_score, required_zone, required_objects, schedule,
                    # cooldown) live in AutoCallConfig per device+camera. We
                    # swallow ALL exceptions — auto-call must never silence
                    # regular notifications.
                    if engine is not None:
                        try:
                            await engine.handle_event(event)
                        except Exception as e:
                            log.error("auto_call.handle_event failed: %s", e)

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

                    send_start = time.time()
                    ok = await sender.send(event)
                    send_ms = int((time.time() - send_start) * 1000)
                    event_age_s = (send_start - float(event["start_time"])) if event.get("start_time") else None
                    age_str = f"event_age={event_age_s:.1f}s " if event_age_s is not None else ""
                    if ok:
                        log.info("  ✓ Delivered %ssend=%dms", age_str, send_ms)
                    else:
                        log.warning("  ✗ Failed %ssend=%dms", age_str, send_ms)

        except aiomqtt.MqttError as e:
            log.warning("MQTT disconnected: %s — reconnecting in %ds", e, retry_delay)
        except Exception as e:
            log.error("Unexpected error: %s — reconnecting in %ds", e, retry_delay)

        await asyncio.sleep(retry_delay)
        retry_delay = min(retry_delay * 2, 30)

    # Cleanup is unreachable in practice (the loop above never exits) but
    # documents intent: when the process is killed, close the HTTP/2 client,
    # the auto-call dispatcher, and tear down the aiohttp sync server.
    await sender.close()
    if voip_dispatcher is not None:
        await voip_dispatcher.aclose()
    if sync_runner is not None:
        await sync_runner.cleanup()


if __name__ == "__main__":
    asyncio.run(run())
