"""In-memory cache of HA discovery entities, populated from MQTT messages.

Lives in process memory only — never written to disk because HA entity
names can be private (e.g. "Chambre Léa"). Repopulated automatically on
MQTT reconnect via retained discovery messages.
"""
import logging
from threading import RLock
from typing import Dict, List

from .parser import DiscoveredEntity, parse_ha_config

log = logging.getLogger("lumen.auto_call.discovery.cache")


class DiscoveryCache:
    def __init__(self) -> None:
        self._entities: Dict[str, DiscoveredEntity] = {}  # key: discovery_topic
        self._lock = RLock()

    def update(self, discovery_topic: str, payload: bytes) -> None:
        """Add/update/remove an entity based on a discovery message.

        - Empty payload on a known topic = remove (HA convention).
        - Non-empty payload, parser yields entity = upsert.
        - Non-empty payload, parser yields None = leave cache untouched.
        """
        if not payload:
            with self._lock:
                self._entities.pop(discovery_topic, None)
            return
        entity = parse_ha_config(discovery_topic, payload)
        if entity is None:
            return
        with self._lock:
            self._entities[discovery_topic] = entity

    def list(self) -> List[DiscoveredEntity]:
        with self._lock:
            entities = list(self._entities.values())
        entities.sort(key=lambda e: (e.name or "").lower())
        return entities

    def topics(self) -> set[str]:
        with self._lock:
            return {e.topic for e in self._entities.values()}
