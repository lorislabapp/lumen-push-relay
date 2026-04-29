"""Button-related ConfigStore tests."""
import json

from src.auto_call.config_store import ConfigStore


def _write(path, data):
    with open(path, "w") as f:
        json.dump(data, f)


def test_loads_buttons_per_token(tmp_path):
    p = tmp_path / "cfg.json"
    _write(str(p), {
        "tokenA": {
            "voipToken": "tokenA",
            "cameras": {},
            "buttons": [
                {"id": "u1", "name": "B1", "topic": "t1",
                 "match": {"path": None, "pattern": {"type": "any"}},
                 "cameras": ["front"], "enabled": True}
            ],
        }
    })
    s = ConfigStore(path=str(p))
    out = s.bindings_matching_topic("t1")
    assert len(out) == 1
    token, b = out[0]
    assert token == "tokenA"
    assert b.id == "u1"


def test_all_button_topics_dedupes(tmp_path):
    p = tmp_path / "cfg.json"
    _write(str(p), {
        "tokenA": {"voipToken": "tokenA", "cameras": {}, "buttons": [
            {"id": "1", "name": "x", "topic": "shared",
             "match": {"pattern": {"type": "any"}}, "cameras": ["c"], "enabled": True},
        ]},
        "tokenB": {"voipToken": "tokenB", "cameras": {}, "buttons": [
            {"id": "2", "name": "y", "topic": "shared",
             "match": {"pattern": {"type": "any"}}, "cameras": ["c"], "enabled": True},
            {"id": "3", "name": "z", "topic": "another",
             "match": {"pattern": {"type": "any"}}, "cameras": ["c"], "enabled": True},
        ]},
    })
    s = ConfigStore(path=str(p))
    assert s.all_button_topics() == {"shared", "another"}


def test_camera_cooldown_for_falls_back_to_default(tmp_path):
    p = tmp_path / "cfg.json"
    _write(str(p), {
        "tokenA": {"voipToken": "tokenA", "cameras": {
            "front": {"enabled": True, "cooldownSeconds": 30}
        }, "buttons": []}
    })
    s = ConfigStore(path=str(p))
    assert s.camera_cooldown_for("tokenA", "front") == 30
    assert s.camera_cooldown_for("tokenA", "unknown") == 60
    assert s.camera_cooldown_for("tokenZ", "front") == 60


def test_malformed_button_entry_skipped(tmp_path):
    p = tmp_path / "cfg.json"
    _write(str(p), {
        "tokenA": {"voipToken": "tokenA", "cameras": {}, "buttons": [
            "not-a-dict",
            {"id": "ok", "name": "good", "topic": "t",
             "match": {"pattern": {"type": "any"}}, "cameras": ["c"], "enabled": True}
        ]}
    })
    s = ConfigStore(path=str(p))
    bound = s.bindings_matching_topic("t")
    assert len(bound) == 1
    assert bound[0][1].id == "ok"
