"""Glue: MQTT events → ConfigStore matching → CooldownStore gate → VoIPDispatcher.

The relay.py main loop calls TriggerEngine.handle_event(event_dict) on each
'frigate/events' new event after its existing processing.
"""
import logging
from typing import Optional

from .config_store import ConfigStore
from .cooldown_store import CooldownStore
from .voip_dispatcher import VoIPDispatcher

logger = logging.getLogger("lumen.auto_call")


class TriggerEngine:
    def __init__(self, config: ConfigStore, dispatcher: VoIPDispatcher, cooldown: CooldownStore) -> None:
        self._config = config
        self._dispatcher = dispatcher
        self._cooldown = cooldown

    def handle_event(self, event: dict) -> int:
        """Returns the number of VoIP pushes dispatched."""
        # Refresh config in case the file changed.
        self._config.reload()
        matches = self._config.matching_dispatches(event)
        dispatched = 0
        for token_cfg, camera, cfg in matches:
            if self._cooldown.is_hot(token_cfg.voip_token, camera, cfg.cooldown_seconds):
                logger.info("auto_call: cooldown hot, skipping camera=%s token=%s...", camera, token_cfg.voip_token[:10])
                continue
            snapshot_url = self._compose_snapshot_url(token_cfg, camera, event)
            ok = self._dispatcher.dispatch(
                device_token_hex=token_cfg.voip_token,
                camera_id=camera,
                camera_display_name=camera,  # display name = id for v1 (app reads its own override)
                snapshot_url=snapshot_url,
                event_id=event.get("id"),
                trigger_type="zoneDetection",  # adjust when physical-button MQTT topic is wired
            )
            if ok:
                self._cooldown.mark_hot(token_cfg.voip_token, camera)
                dispatched += 1
        return dispatched

    def _compose_snapshot_url(self, token_cfg, camera: str, event: dict) -> Optional[str]:
        # Use the user's serverURL + Frigate snapshot path. The signed-URL/CKAsset
        # approach in the original spec is v2 — for v1 we trust that the device
        # is on the same network/Tailscale as Frigate.
        if not token_cfg.server_url or not event.get("id"):
            return None
        return f"{token_cfg.server_url.rstrip('/')}/api/events/{event['id']}/snapshot.jpg"
