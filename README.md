# lumen-push-relay

MQTT-to-push bridge for [Lumen for Frigate](https://apps.apple.com/app/id6760238729). Subscribes to your Frigate MQTT broker, filters detection events, and forwards them through Lumen's Cloudflare relay to Apple Push Notifications (APNs) in under a second.

**Fast (<1s), private (no images leave your network), open source.**

## Why this exists

Frigate publishes detection events over MQTT but does not send HTTP webhooks or push notifications natively. `lumen-push-relay` is the missing bridge — a small Python process that:

1. Subscribes to `frigate/events` on your MQTT broker
2. Filters on label, confidence, camera, zone, schedule, cooldown
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
      MQTT_TOPIC: "frigate/events"
      PUSH_URL: "https://lumen-push.mail5491.workers.dev/v1/notify/YOUR_SECRET/YOUR_TOKEN"
      FILTER_LABELS: "person,car,package"
      FILTER_MIN_SCORE: "0.6"
      COOLDOWN_SECONDS: "120"
    # Optional: mount a config file for per-camera rules
    # volumes:
    #   - ./config.yaml:/config/config.yaml
```

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
| `MQTT_TOPIC` | `frigate/events` | Frigate event topic |
| `PUSH_URL` | — | Your push URL from Lumen (enables worker mode) |
| `FILTER_LABELS` | `person,car,dog,cat,package` | Comma-separated allowed labels |
| `FILTER_MIN_SCORE` | `0.6` | Minimum detection confidence |
| `COOLDOWN_SECONDS` | `120` | Min seconds between pushes for same camera+label |
| `CONFIG_PATH` | `/config/config.yaml` | Path to config file (overrides env vars if present) |

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

## License

MIT — see [LICENSE](LICENSE).

## Related

- [Lumen for Frigate](https://apps.apple.com/app/id6760238729) — native Apple companion app for Frigate NVR
- [lumen-push](https://github.com/lorislabapp/lumen-push) — the Cloudflare Worker that receives events and signs APNs JWTs
- [Frigate NVR](https://github.com/blakeblackshear/frigate) — open-source AI-powered NVR
- [LorisLabs](https://lorislab.fr) — developer website
