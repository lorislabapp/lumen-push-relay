# lumen-push-relay

MQTT-to-push bridge for [Lumen for Frigate](https://apps.apple.com/app/id6760238729). Subscribes to Frigate's Review alerts and forwards them through Lumen's Cloudflare relay to Apple Push Notifications (APNs) in under a second.

**Fast (<1s), private (no images leave your network), open source.**

## Why this exists

Frigate publishes detection events over MQTT but does not send HTTP webhooks or push notifications natively. `lumen-push-relay` is the missing bridge — a small Python process that:

1. Subscribes to `frigate/reviews` on your MQTT broker
2. Filters Review alerts on label, camera, zone, schedule, cooldown
3. Forwards a small JSON payload to Lumen's Cloudflare Worker
4. Worker signs APNs JWTs and delivers the push to your iPhone, iPad, Mac, Watch, or Vision Pro

Only a compact payload (camera name, label, zone, confidence) leaves your network. Camera feeds, snapshots, and clips stay local.

## Prerequisites

You need an **MQTT broker** reachable from the machine that will run `lumen-push-relay`. Frigate needs MQTT enabled in its config anyway, and the relay subscribes to the same broker.

**If you don't have one yet:**

- **Home Assistant users** — install the *Mosquitto broker* add-on (Settings → Add-ons → Add-on Store → Mosquitto broker → Install → Start).
- **Standalone (Docker)** — run Eclipse Mosquitto alongside Frigate:

  ```bash
  docker run -d --name mosquitto --restart unless-stopped \
    -p 1883:1883 \
    eclipse-mosquitto:2 \
    mosquitto -c /mosquitto-no-auth.conf
  ```

Then enable MQTT in your `frigate.yml`:

```yaml
mqtt:
  enabled: true
  host: 192.168.1.50   # your MQTT broker IP (or the container name if on a shared docker network)
  port: 1883
```

Restart Frigate so it picks up the MQTT config.

## Quick start — Docker

```bash
docker run -d \
  --name lumen-push-relay \
  --restart unless-stopped \
  -e MQTT_HOST=192.168.1.50 \
  -e PUSH_URL="paste-your-url-from-lumen-app" \
  lorislabapp/lumen-push-relay
```

Get your `PUSH_URL` from the Lumen app: **Settings → Push Notifications → Copy URL**.

## Docker Compose

```yaml
services:
  lumen-push-relay:
    image: lorislabapp/lumen-push-relay:latest
    container_name: lumen-push-relay
    restart: unless-stopped
    environment:
      MQTT_HOST: "192.168.1.50"
      MQTT_PORT: "1883"
      MQTT_TOPIC: "frigate/reviews"
      PUSH_URL: "https://lumen-push.mail5491.workers.dev/v2/notify/YOUR_CHANNEL_ID"  # copy from Lumen → Instant Push
      FILTER_LABELS: "person,car,package"
      FILTER_MIN_SCORE: "0.6"
      COOLDOWN_SECONDS: "120"
    # Optional: mount a config file for per-camera rules
    # volumes:
    #   - ./config.yaml:/config/config.yaml
```

## Multi-device fan-out (iPhone + Apple Watch, iPad, Mac, Vision Pro)

Lumen 1.17 and later give you **one Push URL for all your devices**:
`https://lumen-push.mail5491.workers.dev/v2/notify/<channel ID>`. Every device signed in to the same iCloud account registers on that channel, and the Worker delivers each Frigate event to all of them. Paste that single URL into `PUSH_URL` — no per-device URLs needed.

If you regenerate the URL in Lumen, update `PUSH_URL` too. Use **Send Test Notification** in Lumen to check the channel: it reports whether the push server has a device on it.

**The standalone Apple Watch app** (watchOS 11+) registers on the same channel, so it receives Frigate push directly over cellular, even with the paired iPhone out of range or powered off.

### Older app versions (v1 URLs)

Before 1.17, each device gave its own URL of the form `…/v1/notify/<secret>/<device token>`. Those URLs still work. To send to several of them, add `PUSH_URL_2`, `PUSH_URL_3`, … (up to 32), or pass a comma-separated list via `PUSH_URLS`:

```yaml
services:
  lumen-push-relay:
    image: lorislabapp/lumen-push-relay:latest
    environment:
      MQTT_HOST: "192.168.1.50"
      PUSH_URL:   "URL copied from the iPhone"
      PUSH_URL_2: "URL copied from the Apple Watch"
```

Or a single variable:

```yaml
      PUSH_URLS: "URL_IPHONE,URL_WATCH,URL_MAC"
```

`PUSH_URLS` takes precedence if both forms are set. Filters and cooldown run **once** before fan-out, so you still get at most one push per event.

## Per-camera rules (advanced)

For zone filtering, schedules, custom messages, and per-camera overrides, mount a `config.yaml` at `/config/config.yaml`. See [`config.yaml.example`](config.yaml.example) for the full schema.

Example:

```yaml
filters:
  labels: [person, car]
  min_score: 0.6
  cooldown_seconds: 120
  cameras:
    front_door:
      required_zones: [porch]
      zone_messages:
        mailbox:
          title: "Mail activity"
          body: "Someone was spotted at the mailbox"
    driveway:
      schedule: "22:00-07:00"   # only at night
```

## Python setup (without Docker)

```bash
git clone https://github.com/lorislabapp/lumen-push-relay.git
cd lumen-push-relay
pip install -r requirements.txt
cp config.yaml.example config.yaml   # edit to match your setup
python3 relay.py
```

A systemd unit file is included at [`frigate-apns-relay.service`](frigate-apns-relay.service).

## Modes

- **`worker` mode** (recommended) — forwards events to Lumen's Cloudflare Worker, which handles APNs signing. No APNs key required. Requires internet.
- **`direct` mode** — signs APNs JWTs locally and POSTs directly to Apple. Requires an APNs key (.p8), Team ID, Key ID, Bundle ID, and device tokens. Works on a LAN with internet only to reach `api.push.apple.com`.

Set `mode: worker` (default) or `mode: direct` in `config.yaml`, or omit `PUSH_URL` and set the `APNS_*` env vars to pick direct mode.

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `MQTT_HOST` | `localhost` | MQTT broker hostname/IP |
| `MQTT_PORT` | `1883` | MQTT broker port |
| `MQTT_USER` | — | MQTT username (optional) |
| `MQTT_PASSWORD` | — | MQTT password (optional) |
| `MQTT_TOPIC` | `frigate/reviews` | Frigate Review topic |
| `REVIEW_SEVERITIES` | `alert` | Review severities to notify (`alert,detection` to include both). `FILTER_REVIEW_SEVERITIES` is accepted as a deprecated alias; `REVIEW_SEVERITIES` wins when both are set |
| `ALLOW_RAW_EVENTS` | `false` | Explicit compatibility opt-in for `*/events`; may notify objects absent from Review |
| `PUSH_URL` | — | Your push URL from Lumen (enables worker mode) |
| `FILTER_LABELS` | `person,car,dog,cat,package` | Comma-separated allowed labels |
| `FILTER_MIN_SCORE` | `0.6` | Minimum detection confidence |
| `COOLDOWN_SECONDS` | `120` | Min seconds between pushes for same camera+label |
| `CONFIG_PATH` | `/config/config.yaml` | Path to config file (overrides env vars if present) |
| `FRIGATE_URL` | — | Optional. Frigate API base URL (e.g. `http://192.168.1.50:5000`). Enables camera display names (`friendly_name`) in notifications |
| `FRIGATE_TOKEN` | — | Optional. Bearer token sent to `FRIGATE_URL` (e.g. behind an authenticating proxy) |
| `FRIGATE_USER` / `FRIGATE_PASSWORD` | — | Optional. Frigate login used when port 8971 authentication is on |
| `FRIGATE_TLS_VERIFY` | `true` | Set `false` only for a self-signed Frigate certificate |

### Camera display names (optional, 1.2.2+)

When `FRIGATE_URL` is set, the relay reads `/api/config` at start and every 10 minutes and adds `camera_display_name` (Frigate `friendly_name`, max 64 characters) to each notification. If Frigate is unreachable or the camera has no `friendly_name`, the field is simply left out — delivery never waits for it. In a config file use `frigate.url`, `frigate.api_token`, `frigate.api_user`, `frigate.api_password`, `frigate.tls_verify`.

### Direct mode only

| Variable | Description |
|---|---|
| `APNS_KEY_FILE` | Path to your APNs .p8 key |
| `APNS_KEY_ID` | Key ID from Apple Developer portal |
| `APNS_TEAM_ID` | Your Apple Developer Team ID |
| `APNS_BUNDLE_ID` | Usually `com.lorislab.lumenforfrigate.Lumen-for-Frigate` |
| `APNS_ENVIRONMENT` | `production` or `sandbox` |
| `DEVICE_TOKENS` | Comma-separated device tokens |

## Troubleshooting

**No notifications**  
Check Frigate has MQTT enabled (`mqtt.enabled: true` in `frigate.yml`) and that the relay container can reach the broker (`docker logs lumen-push-relay`). Look for `Connected to MQTT, listening for events...`.

**Notifications delayed**  
The relay forwards events in <100 ms on a LAN. If you see delays, check APNs latency (`api.push.apple.com` should respond in <500 ms) and make sure your device has notification permission for Lumen.

**Events logged but no push**  
The worker mode returns 200 even if APNs drops the push. Check the worker logs (`wrangler tail` if self-hosting) or send a test push from Lumen's Settings → Push Notifications → Send Test.

## Late confidence and Review end (1.2.2)

Frigate often publishes the final confidence of an object only on its `end` message. The relay now uses `end` messages for both events and Reviews. A Review that is still open stays eligible for up to 15 minutes, as long as the detection itself shows fresh activity (`FILTER_MAX_EVENT_AGE`, default 120 s, now measured from Frigate's latest frame/end time). A Review is sent at most once per severity. When a Review closes without being sent, the log says why: `below_min_score`, `label_filtered`, `stationary`, `severity`, `expired`, `camera_filtered`, `zone_filtered`, `schedule`.

Newer lumen-push Workers answer with per-device outcomes. When nothing was delivered because of a rate limit, the relay retries once after 6 s. A push the Worker deliberately suppressed (duplicate, filtered, cooldown) is recorded as handled and is not retried. Older Workers answering `OK` work as before.

## License

MIT — see [LICENSE](LICENSE).

## Related

- [Lumen for Frigate](https://apps.apple.com/app/id6760238729) — native Apple companion app for Frigate NVR
- [lumen-push](https://github.com/lorislabapp/lumen-push) — the Cloudflare Worker that receives events and signs APNs JWTs
- [Frigate NVR](https://github.com/blakeblackshear/frigate) — open-source AI-powered NVR
- [LorisLabs](https://lorislab.fr) — developer website

## Review correlation update

Notifications now require a genuine Frigate Review item correlated with its camera-consistent object detection. Object events alone never authorize a notification. Confidence is Frigate top_score; missing or invalid scores cannot pass a configured threshold. Default Review severity remains alert. Existing object/camera/zone/schedule filters apply before delivery.

The MQTT identity must allow read access to the configured prefix's events, reviews and tracked_object_update topics (normally frigate/events, frigate/reviews and frigate/tracked_object_update). Custom prefixes are preserved; LUMEN_EVENT_TOPIC, LUMEN_REVIEW_TOPIC and LUMEN_IDENTITY_TOPIC can override the individual topics. ALLOW_RAW_EVENTS no longer bypasses correlation in the delivery loop.

Mount persistent storage at /config to preserve Review delivery state across restarts; LUMEN_REVIEW_STATE_PATH can select another writable path. Failed delivery does not consume successful-delivery deduplication or local cooldown. Concurrent or cross-region exactly-once delivery is not guaranteed.

For Lumen's Review-alert-only setting, use this updated relay and confirm that the app's supported delivery filters are synchronized with the push service. An old raw-object relay cannot supply Review evidence and will be suppressed by that option. Example local policy: FILTER_LABELS=person,car,cat, FILTER_MIN_SCORE=0.8, REVIEW_SEVERITIES=alert and COOLDOWN_SECONDS=120. Adjust to your installation. GenAI descriptions are not interpreted as motion state.
