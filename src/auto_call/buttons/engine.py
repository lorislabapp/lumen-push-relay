"""ButtonEngine — non-Frigate MQTT message → match → cooldown → VoIP dispatch.

Wired into the relay's TopicRouter; called for every MQTT message whose
topic is not the Frigate events topic and not under the HA discovery prefix.
"""
import logging
import time
from typing import Protocol

from .match import MatchEvaluator

log = logging.getLogger("lumen.auto_call.buttons.engine")


class _ConfigStoreLike(Protocol):
    def bindings_matching_topic(self, topic: str) -> list[tuple[str, "PhysicalButtonBinding"]]: ...
    def camera_cooldown_for(self, token: str, camera_id: str) -> int: ...


class _DispatcherLike(Protocol):
    async def dispatch(self, *, device_token_hex: str, camera_id: str,
                       camera_display_name: str, snapshot_url: str | None,
                       event_id: str, trigger_type: str) -> bool: ...


class _CooldownLike(Protocol):
    def is_hot(self, token: str, camera: str, seconds: int) -> bool: ...
    def mark_hot(self, token: str, camera: str) -> None: ...


class ButtonEngine:
    def __init__(self, config_store: _ConfigStoreLike,
                 dispatcher: _DispatcherLike,
                 cooldown: _CooldownLike,
                 evaluator: MatchEvaluator | None = None) -> None:
        self._cs = config_store
        self._dispatcher = dispatcher
        self._cooldown = cooldown
        self._matcher = evaluator or MatchEvaluator()

    async def handle_message(self, topic: str, payload: bytes) -> int:
        bindings = self._cs.bindings_matching_topic(topic)
        if not bindings:
            return 0
        dispatched = 0
        for token, binding in bindings:
            if not binding.enabled:
                continue
            if not self._matcher.matches(binding.match, payload):
                continue
            for camera_id in binding.camera_ids:
                cooldown_s = self._cs.camera_cooldown_for(token, camera_id)
                if self._cooldown.is_hot(token, camera_id, cooldown_s):
                    log.debug("buttons: cooldown hot token=%s... camera=%s",
                              token[:10], camera_id)
                    continue
                try:
                    ok = await self._dispatcher.dispatch(
                        device_token_hex=token,
                        camera_id=camera_id,
                        camera_display_name=camera_id,
                        snapshot_url=None,
                        event_id=f"button-{binding.id}-{camera_id}-{int(time.time())}",
                        trigger_type="physicalButton",
                    )
                except Exception:
                    log.exception("buttons: dispatcher raised for token=%s... camera=%s",
                                  token[:10], camera_id)
                    continue
                if ok:
                    log.info("buttons: dispatched binding=%s camera=%s token=%s...",
                             binding.id, camera_id, token[:10])
                    self._cooldown.mark_hot(token, camera_id)
                    dispatched += 1
                else:
                    log.warning("buttons: dispatch returned False for token=%s... camera=%s",
                                token[:10], camera_id)
        return dispatched
