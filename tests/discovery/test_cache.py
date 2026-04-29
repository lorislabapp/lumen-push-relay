import json

from src.auto_call.discovery.cache import DiscoveryCache


def cfg(d): return json.dumps(d).encode()


def test_update_adds_entity():
    c = DiscoveryCache()
    c.update("homeassistant/button/btn1/config",
             cfg({"name": "Btn1", "command_topic": "x"}))
    items = c.list()
    assert len(items) == 1
    assert items[0].object_id == "btn1"


def test_update_unknown_component_ignored():
    c = DiscoveryCache()
    c.update("homeassistant/sensor/x/config", cfg({"name": "X", "state_topic": "y"}))
    assert c.list() == []


def test_update_overwrites_existing_entity():
    c = DiscoveryCache()
    c.update("homeassistant/button/btn1/config",
             cfg({"name": "Old", "command_topic": "x"}))
    c.update("homeassistant/button/btn1/config",
             cfg({"name": "New", "command_topic": "x"}))
    items = c.list()
    assert len(items) == 1
    assert items[0].name == "New"


def test_empty_payload_removes_entity():
    c = DiscoveryCache()
    c.update("homeassistant/button/btn1/config",
             cfg({"name": "Btn1", "command_topic": "x"}))
    c.update("homeassistant/button/btn1/config", b"")
    assert c.list() == []


def test_list_sorted_by_name():
    c = DiscoveryCache()
    c.update("homeassistant/button/a/config",
             cfg({"name": "Zeta", "command_topic": "x"}))
    c.update("homeassistant/button/b/config",
             cfg({"name": "Alpha", "command_topic": "y"}))
    names = [e.name for e in c.list()]
    assert names == ["Alpha", "Zeta"]


def test_topics_returns_set_of_known_topics():
    c = DiscoveryCache()
    c.update("homeassistant/button/a/config",
             cfg({"name": "A", "command_topic": "topic/a"}))
    c.update("homeassistant/button/b/config",
             cfg({"name": "B", "command_topic": "topic/b"}))
    assert c.topics() == {"topic/a", "topic/b"}
