"""APNs VoIP push dispatcher.

Reuses the existing .p8 JWT signing logic (see DirectAPNsSender._get_token in
relay.py — the JWT provider here is any callable returning a current bearer
JWT string). The only differences from regular pushes:
  - apns-topic: <bundleID>.voip  (suffix .voip)
  - apns-push-type: voip
  - apns-priority: 10
  - apns-expiration: 0 (drop if not deliverable in seconds)
"""
import json
import logging
import os
from typing import Optional

import httpx  # already in requirements.txt for direct-mode APNs

logger = logging.getLogger("lumen.auto_call.voip")

BUNDLE_ID = os.environ.get("APP_BUNDLE_ID", "com.lorislab.lumenforfrigate.Lumen-for-Frigate")
APNS_HOST = os.environ.get("APNS_HOST", "https://api.push.apple.com")  # use api.sandbox.push.apple.com for dev


class VoIPDispatcher:
    def __init__(self, jwt_provider) -> None:
        """jwt_provider is a callable returning the current JWT string. Re-use whatever
        the existing relay.py uses for direct-mode APNs sends."""
        self._jwt_provider = jwt_provider
        self._client = httpx.Client(http2=True, timeout=5.0)

    def dispatch(
        self,
        device_token_hex: str,
        camera_id: str,
        camera_display_name: str,
        snapshot_url: Optional[str] = None,
        event_id: Optional[str] = None,
        trigger_type: str = "zoneDetection",
    ) -> bool:
        topic = f"{BUNDLE_ID}.voip"
        payload = {
            "aps": {"content-available": 1},
            "cameraId": camera_id,
            "cameraDisplayName": camera_display_name,
            "triggerType": trigger_type,
        }
        if snapshot_url:
            payload["snapshotURL"] = snapshot_url
        if event_id:
            payload["eventId"] = event_id

        url = f"{APNS_HOST}/3/device/{device_token_hex}"
        headers = {
            "authorization": f"bearer {self._jwt_provider()}",
            "apns-topic": topic,
            "apns-push-type": "voip",
            "apns-priority": "10",
            "apns-expiration": "0",
            "content-type": "application/json",
        }
        try:
            r = self._client.post(url, headers=headers, content=json.dumps(payload).encode())
        except Exception as e:
            logger.error("VoIP dispatch failed (network): %s", e)
            return False
        if r.status_code == 200:
            logger.info("VoIP dispatched camera=%s token=%s...", camera_id, device_token_hex[:10])
            return True
        logger.warning("VoIP dispatch HTTP %d body=%s", r.status_code, r.text[:200])
        return False
