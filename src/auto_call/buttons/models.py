"""Wire-format dataclasses for physical-button bindings."""
from dataclasses import dataclass
from typing import List, Optional


@dataclass
class MatchRule:
    path: Optional[str]                 # JSONPath subset; None = full payload string
    pattern_type: str                   # "any" | "equals" | "regex"
    pattern_value: Optional[str]        # None for "any"


@dataclass
class PhysicalButtonBinding:
    id: str                             # UUID string
    name: str
    topic: str
    match: MatchRule
    camera_ids: List[str]
    enabled: bool


def parse_binding(raw: dict) -> PhysicalButtonBinding:
    """Parse one binding dict from the iOS sync payload.

    Wire shape (one element of `buttons[]` array):
        {
          "id": "<uuid>", "name": "...", "topic": "...",
          "match": { "path": "$.action", "pattern": {"type": "equals", "value": "single"} },
          "cameras": ["front", "porch"], "enabled": true
        }
    """
    match_raw = raw.get("match") or {}
    pat_raw = match_raw.get("pattern") or {}
    rule = MatchRule(
        path=match_raw.get("path"),
        pattern_type=str(pat_raw.get("type", "any")),
        pattern_value=pat_raw.get("value"),
    )
    return PhysicalButtonBinding(
        id=str(raw.get("id", "")),
        name=str(raw.get("name", "")),
        topic=str(raw.get("topic", "")),
        match=rule,
        camera_ids=list(raw.get("cameras") or []),
        enabled=bool(raw.get("enabled", True)),
    )
