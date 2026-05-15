# lumen-push-relay Deployment Guide

## Version 1.1.0 — Phase 1 Quick Wins

This version includes:
- ✅ Health monitoring endpoint (`/health`)
- ✅ TCP keep-alive tuning (15s MQTT ping)
- ✅ Snapshot pre-compression (Pillow → ~40KB JPEG before relay→Worker)
- ✅ Exponential backoff reconnect (5s → 10s → 20s → 40s)

## Prerequisites

- Docker or Python 3.11+
- MQTT broker (Mosquitto recommended)
- Frigate instance with MQTT enabled
- Lumen for Frigate installed on iPhone/iPad/Mac/Watch/Vision Pro

## Option 1: Docker Deployment (Recommended)

### 1. Build the image

```bash
cd lumen-push-relay
docker build -t lorislabapp/lumen-push-relay:1.1.0 .
docker tag lorislabapp/lumen-push-relay:1.1.0 lorislabapp/lumen-push-relay:latest
```

### 2. Push to Docker Hub (maintainers only)

```bash
docker login
docker push lorislabapp/lumen-push-relay:1.1.0
docker push lorislabapp/lumen-push-relay:latest
```

### 3. Run the container

```bash
docker run -d \
  --name lumen-push-relay \
  --restart unless-stopped \
  -e MQTT_HOST=192.168.1.50 \
  -e FRIGATE_URL=http://192.168.1.50:5000 \
  -e PUSH_URL="<your-url-from-lumen-app>" \
  lorislabapp/lumen-push-relay:1.1.0
```

Get `PUSH_URL` from: **Lumen app → Settings → Push Notifications → Copy URL**

### 4. Verify health

```bash
# Check logs
docker logs -f lumen-push-relay

# Check health endpoint (should show "ok" status after ~5 seconds)
curl http://localhost:8765/health | jq
```

You should see:
```json
{
  "status": "ok",
  "mqtt": {
    "connected": true
  },
  "uptime_seconds": 123
}
```

## Option 2: Docker Compose

Create `docker-compose.yml`:

```yaml
services:
  lumen-push-relay:
    image: lorislabapp/lumen-push-relay:1.1.0
    container_name: lumen-push-relay
    restart: unless-stopped
    environment:
      MQTT_HOST: "192.168.1.50"
      MQTT_PORT: "1883"
      MQTT_TOPIC: "frigate/events"
      FRIGATE_URL: "http://192.168.1.50:5000"
      PUSH_URL: "https://lumen-push.mail5491.workers.dev/v1/notify/YOUR_SECRET/YOUR_TOKEN"
      FILTER_LABELS: "person,car,package"
      FILTER_MIN_SCORE: "0.6"
      COOLDOWN_SECONDS: "120"
    ports:
      - "8765:8765"  # Health endpoint + auto-call sync
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8765/health"]
      interval: 60s
      timeout: 5s
      retries: 3
      start_period: 10s
```

Deploy:
```bash
docker compose up -d
docker compose logs -f
```

## Option 3: Python (systemd service)

### 1. Install dependencies

```bash
git clone https://github.com/lorislabapp/lumen-push-relay.git
cd lumen-push-relay
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Create environment file

```bash
sudo mkdir -p /etc/lumen-push-relay
sudo nano /etc/lumen-push-relay/relay.env
```

Add:
```
MQTT_HOST=192.168.1.50
MQTT_PORT=1883
FRIGATE_URL=http://192.168.1.50:5000
PUSH_URL=https://lumen-push.mail5491.workers.dev/v1/notify/YOUR_SECRET/YOUR_TOKEN
FILTER_LABELS=person,car,package
FILTER_MIN_SCORE=0.6
COOLDOWN_SECONDS=120
```

### 3. Install systemd service

```bash
# Edit frigate-apns-relay.service to match your paths
sudo cp frigate-apns-relay.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable frigate-apns-relay
sudo systemctl start frigate-apns-relay
```

### 4. Check status

```bash
sudo systemctl status frigate-apns-relay
curl http://localhost:8765/health | jq
```

## Option 4: Proxmox LXC (Advanced)

For deployment in a Proxmox LXC container:

### 1. Create Ubuntu 24.04 LXC

```bash
# On Proxmox host
pct create 199 local:vztmpl/ubuntu-24.04-standard_24.04-2_amd64.tar.zst \
  --hostname lumen-push-relay \
  --memory 512 \
  --swap 512 \
  --cores 1 \
  --net0 name=eth0,bridge=vmbr0,ip=dhcp \
  --storage local-lvm \
  --unprivileged 1 \
  --features nesting=1

pct start 199
pct enter 199
```

### 2. Inside LXC: Install Python and dependencies

```bash
apt update && apt install -y python3 python3-pip python3-venv git curl
git clone https://github.com/lorislabapp/lumen-push-relay.git /opt/lumen-push-relay
cd /opt/lumen-push-relay
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 3. Configure and run

Follow **Option 3** systemd steps above.

## Configuring Lumen App

After relay is running:

1. Open **Lumen for Frigate**
2. Go to **Settings → Push Relay Health**
3. Enter relay URL: `http://192.168.1.100:8765/health` (replace with your relay IP)
4. Tap **Check Now** → Should show **Online** status
5. Banner appears if relay goes offline

## Monitoring

### Health endpoint

```bash
curl http://relay-ip:8765/health
```

Returns:
- `status: "ok"` — MQTT connected, relay healthy
- `status: "degraded"` — MQTT disconnected, attempting reconnect
- `uptime_seconds` — Relay uptime
- `events.total_received` — Events seen from Frigate
- `events.delivered` — Pushes successfully sent
- `events.failed` — Failed deliveries
- `last_event_ago_seconds` — Time since last event (useful for debugging)

### Logs

**Docker:**
```bash
docker logs -f lumen-push-relay
```

**Systemd:**
```bash
sudo journalctl -u frigate-apns-relay -f
```

### What to look for

✅ **Good:**
```
Connected to MQTT broker at 192.168.1.50:1883
Listening for events on frigate/events
Event received: front_door person (0.87)
Compressed snapshot: 1024x576 → 512x288, 156KB → 38KB
Delivered push to 1 device(s)
```

❌ **Problems:**
```
# MQTT connection issues
Failed to connect to MQTT: [Errno 111] Connection refused

# Frigate not publishing
Connected to MQTT, listening for events...
(nothing for >10 minutes)
→ Check Frigate has `mqtt.enabled: true`

# Push delivery failures
Failed to deliver push: 403 Forbidden
→ Check PUSH_URL is correct (copy from app)
```

## Updating to 1.1.0

### Docker

```bash
docker pull lorislabapp/lumen-push-relay:1.1.0
docker stop lumen-push-relay
docker rm lumen-push-relay
# Re-run docker run command from above
```

### Docker Compose

```bash
docker compose pull
docker compose up -d
```

### Systemd

```bash
cd /opt/lumen-push-relay
git pull origin main
source venv/bin/activate
pip install -r requirements.txt --upgrade
sudo systemctl restart frigate-apns-relay
```

## Rollback

If 1.1.0 causes issues, roll back to 1.0.0:

```bash
docker run -d \
  --name lumen-push-relay \
  --restart unless-stopped \
  -e MQTT_HOST=... \
  lorislabapp/lumen-push-relay:1.0.0
```

## Troubleshooting

**Problem: Notifications delayed (was <1s, now 3-5s)**

Check:
```bash
curl http://relay-ip:8765/health
```

If `mqtt.connected: false`, check:
- MQTT broker is running: `docker logs mosquitto` or `systemctl status mosquitto`
- Relay can reach broker: `telnet mqtt-host 1883` from relay container/host
- MQTT credentials correct (if using auth)

**Problem: No snapshots in notifications**

Check Frigate URL is set:
```bash
docker inspect lumen-push-relay | grep FRIGATE_URL
```

If empty, add:
```bash
docker stop lumen-push-relay
docker rm lumen-push-relay
docker run -d ... -e FRIGATE_URL=http://192.168.1.50:5000 ...
```

**Problem: Health endpoint returns 404**

Old version (<1.1.0). Update to 1.1.0.

**Problem: Relay crashes with "ModuleNotFoundError: No module named 'PIL'"**

Pillow not installed. Update requirements:
```bash
pip install -r requirements.txt --upgrade
```

## Performance Benchmarks (Phase 1 Quick Wins)

| Metric | Before (1.0.0) | After (1.1.0) | Improvement |
|--------|----------------|---------------|-------------|
| MQTT disconnect detection | 60s | 15s | **4×** |
| NSE thumbnail fetch | ~500ms (Frigate) | ~0-50ms (embedded/KV) | **10-100×** |
| Relay restart delay | 30-60s (random backoff) | 5-40s (exponential) | **Predictable** |
| Relay health visibility | None | <3 min (active polling) | **New feature** |

## Security Notes

- The `/health` endpoint exposes no sensitive data (camera names, event details, tokens)
- Snapshot compression happens **before** leaving your network
- Worker receives only compressed JPEG bytes (base64), not original snapshots
- PUSH_URL contains your device token but not APNs signing key (that's Worker-side)

## Support

- GitHub Issues: https://github.com/lorislabapp/lumen-push-relay/issues
- Lumen support: support@lorislab.fr
- Documentation: https://lorislab.fr/lumen-push-relay.html (coming soon)
