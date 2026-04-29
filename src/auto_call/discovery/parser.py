"""Parse Home Assistant MQTT discovery config payloads.

HA publishes config to `homeassistant/<component>/<object_id>/config`
(or `<discovery_prefix>/<component>/<node_id>/<object_id>/config`).
We index the 4 button-shaped components.
"""
import json
import logging
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger("lumen.auto_call.discovery.parser")

RELEVANT_COMPONENTS = {"binary_sensor", "button", "device_automation", "event"}


@dataclass
class DiscoveredEntity:
    object_id: str
    name: str
    topic: str
    device_class: Optional[str]
    suggested_match: dict           # serializable match dict (matches MatchRule wire shape)


def parse_ha_config(discovery_topic: str, payload: bytes) -> Optional[DiscoveredEntity]:
    parts = discovery_topic.split("/")
    # Expect: prefix / component / [node_id /] object_id / config
    # Minimum 4 parts: "homeassistant/<component>/<object_id>/config".
    if len(parts) < 4 or parts[-1] != "config":
        return None
    component = parts[1]
    object_id = parts[-2]
    if component not in RELEVANT_COMPONENTS:
        return None
    if not payload:
        return None  # HA convention: empty retained = entity removed
    try:
        data = json.loads(payload)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None

    name = data.get("name") or object_id
    device_class = data.get("device_class")
    topic = data.get("topic") or data.get("state_topic") or data.get("command_topic")
    if not topic:
        return None

    suggested = _suggest_match(component, device_class, data)
    return DiscoveredEntity(
        object_id=object_id, name=name, topic=topic,
        device_class=device_class, suggested_match=suggested,
    )


def _suggest_match(component: str, device_class: Optional[str], data: dict) -> dict:
    """Return a serializable MatchRule-shaped dict for the picker UX."""
    if component == "button":
        return {"path": None, "pattern_type": "any", "pattern_value": None}
    if component == "binary_sensor":
        return {"path": None, "pattern_type": "equals", "pattern_value": "ON"}
    if component == "device_automation":
        # Z2M Aqara typically: subtype="single"|"double"|"hold". Default to "single".
        subtype = data.get("subtype") or "single"
        return {"path": "$.action", "pattern_type": "equals", "pattern_value": str(subtype)}
    if component == "event":
        first = ""
        ev_types = data.get("event_types")
        if isinstance(ev_types, list) and ev_types:
            first = str(ev_types[0])
        return {"path": "$.event_type", "pattern_type": "equals", "pattern_value": first}
    return {"path": None, "pattern_type": "any", "pattern_value": None}
