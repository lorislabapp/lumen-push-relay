"""MQTT subscription orchestration + topic-based message routing."""
import logging
from typing import Awaitable, Callable, Protocol

log = logging.getLogger("lumen.auto_call.mqtt")


class _MQTTLike(Protocol):
    async def subscribe(self, topic: str): ...
    async def unsubscribe(self, topic: str): ...


class SubscriptionManager:
    """Idempotent reconcile between desired-topics set and actually-subscribed set."""

    def __init__(self, mqtt: _MQTTLike) -> None:
        self._mqtt = mqtt
        self._current: set[str] = set()

    async def reconcile(self, desired: set[str]) -> None:
        to_add = desired - self._current
        to_remove = self._current - desired
        for t in sorted(to_add):
            try:
                await self._mqtt.subscribe(t)
            except Exception as e:
                log.error("subscribe %s failed: %s", t, e)
                continue
            self._current.add(t)
        for t in sorted(to_remove):
            try:
                await self._mqtt.unsubscribe(t)
            except Exception as e:
                log.error("unsubscribe %s failed: %s", t, e)
                continue
            self._current.discard(t)


FrigateHandler = Callable[[str, bytes], Awaitable[None]]


class _DiscoveryCacheLike(Protocol):
    def update(self, topic: str, payload: bytes) -> None: ...


class _ButtonEngineLike(Protocol):
    async def handle_message(self, topic: str, payload: bytes) -> int: ...


class TopicRouter:
    """Single dispatch point for every MQTT message in the relay loop.

    Layered try/except per branch so one handler raising never prevents
    the others from running on subsequent messages.
    """

    def __init__(
        self,
        frigate_topic: str,
        ha_prefix: str,
        frigate_handler: FrigateHandler,
        discovery_cache: _DiscoveryCacheLike,
        button_engine: _ButtonEngineLike,
    ) -> None:
        self._frigate_topic = frigate_topic
        self._ha_prefix = ha_prefix.rstrip("/") + "/"
        self._frigate = frigate_handler
        self._cache = discovery_cache
        self._engine = button_engine

    async def route(self, topic: str, payload: bytes) -> None:
        try:
            if topic == self._frigate_topic:
                await self._frigate(topic, payload)
            elif topic.startswith(self._ha_prefix):
                self._cache.update(topic, payload)
            else:
                await self._engine.handle_message(topic, payload)
        except Exception as e:
            log.error("router: handler raised topic=%s err=%s", topic, e)
