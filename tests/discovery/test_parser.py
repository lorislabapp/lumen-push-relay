"""Tests for HA discovery parser."""
import json

import pytest

from src.auto_call.discovery.parser import parse_ha_config


def cfg(d):
    return json.dumps(d).encode()


def test_button_component():
    e = parse_ha_config("homeassistant/button/aqara_btn/config",
                        cfg({"name": "Aqara Button", "command_topic": "zigbee2mqtt/aqara/cmd"}))
    assert e is not None
    assert e.object_id == "aqara_btn"
    assert e.name == "Aqara Button"
    assert e.topic == "zigbee2mqtt/aqara/cmd"
    assert e.suggested_match == {"path": None, "pattern_type": "any", "pattern_value": None}


def test_binary_sensor_door_class_suggests_state_on():
    e = parse_ha_config("homeassistant/binary_sensor/front_door/config",
                        cfg({"name": "Front Door", "state_topic": "ha/front_door/state",
                             "device_class": "door"}))
    assert e is not None
    assert e.topic == "ha/front_door/state"
    assert e.suggested_match == {"path": None, "pattern_type": "equals", "pattern_value": "ON"}


def test_device_automation_aqara():
    e = parse_ha_config("homeassistant/device_automation/aqara_action/config",
                        cfg({"name": "Aqara Action", "topic": "zigbee2mqtt/aqara/action",
                             "automation_type": "trigger", "type": "action", "subtype": "single"}))
    assert e is not None
    assert e.topic == "zigbee2mqtt/aqara/action"
    # Suggest match action == "single" via JSONPath.
    assert e.suggested_match["path"] == "$.action"
    assert e.suggested_match["pattern_type"] == "equals"


def test_event_component():
    e = parse_ha_config("homeassistant/event/doorbell_event/config",
                        cfg({"name": "Doorbell", "state_topic": "ha/doorbell/state",
                             "event_types": ["pressed", "released"]}))
    assert e is not None
    # event suggested match: jsonpath on event_type with first event_types value.
    assert e.suggested_match["path"] == "$.event_type"


def test_unknown_component_returns_none():
    e = parse_ha_config("homeassistant/sensor/temp/config",
                        cfg({"name": "Temp", "state_topic": "x"}))
    assert e is None


def test_empty_payload_returns_none():
    # Empty payload = HA convention for entity removal — caller handles it.
    e = parse_ha_config("homeassistant/button/x/config", b"")
    assert e is None


def test_malformed_json_returns_none():
    e = parse_ha_config("homeassistant/button/x/config", b"not json")
    assert e is None


def test_missing_topic_field_returns_none():
    e = parse_ha_config("homeassistant/button/x/config", cfg({"name": "X"}))
    assert e is None


def test_falls_back_to_object_id_when_name_absent():
    e = parse_ha_config("homeassistant/button/my_btn/config",
                        cfg({"command_topic": "x"}))
    assert e is not None
    assert e.name == "my_btn"


def test_short_discovery_topic_returns_none():
    e = parse_ha_config("homeassistant/button/config", b'{"name":"x","command_topic":"y"}')
    assert e is None
