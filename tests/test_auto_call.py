"""Tests for the auto-call subsystem."""
import json
import os
import tempfile
import time
from unittest.mock import MagicMock

import pytest

from src.auto_call import ConfigStore, CooldownStore, TriggerEngine, VoIPDispatcher


def make_config_file(data: dict) -> str:
    path = tempfile.mktemp(suffix=".json")
    with open(path, "w") as f:
        json.dump(data, f)
    return path


def test_cooldown_hot_then_cold():
    s = CooldownStore()
    assert not s.is_hot("tok", "front_door", 60)
    s.mark_hot("tok", "front_door")
    assert s.is_hot("tok", "front_door", 60)
    s.clear()
    assert not s.is_hot("tok", "front_door", 60)


def test_cooldown_zero_seconds_never_hot():
    s = CooldownStore()
    s.mark_hot("tok", "front_door")
    assert not s.is_hot("tok", "front_door", 0)


def test_config_loads_minimal_camera():
    path = make_config_file({
        "abcdef123": {
            "voipToken": "abcdef123",
            "serverURL": "http://10.9.8.209:5000",
            "cameras": {
                "front_door": {
                    "enabled": True,
                    "trigger": "zoneDetection",
                    "requiredZoneId": "porch",
                    "minScore": 0.85,
                    "cooldownSeconds": 60,
                    "requiredObjects": ["person"],
                    "manualCallEnabled": True,
                }
            },
        }
    })
    s = ConfigStore(path=path)
    matches = s.matching_dispatches({
        "camera": "front_door",
        "label": "person",
        "top_score": 0.92,
        "current_zones": ["porch"],
        "id": "evt-1",
    })
    assert len(matches) == 1
    _, cam, cfg = matches[0]
    assert cam == "front_door"
    assert cfg.enabled is True
    os.unlink(path)


def test_config_filters_by_score():
    path = make_config_file({
        "tok": {
            "voipToken": "tok",
            "cameras": {
                "front_door": {"enabled": True, "minScore": 0.95, "requiredObjects": ["person"]}
            },
        }
    })
    s = ConfigStore(path=path)
    matches = s.matching_dispatches({"camera": "front_door", "label": "person", "top_score": 0.80, "current_zones": []})
    assert matches == []
    os.unlink(path)


def test_config_filters_by_zone():
    path = make_config_file({
        "tok": {
            "voipToken": "tok",
            "cameras": {
                "front_door": {
                    "enabled": True, "minScore": 0.5, "requiredZoneId": "porch",
                    "requiredObjects": ["person"]
                }
            },
        }
    })
    s = ConfigStore(path=path)
    matches = s.matching_dispatches({"camera": "front_door", "label": "person", "top_score": 0.9, "current_zones": ["driveway"]})
    assert matches == []
    matches = s.matching_dispatches({"camera": "front_door", "label": "person", "top_score": 0.9, "current_zones": ["porch"]})
    assert len(matches) == 1
    os.unlink(path)


def test_trigger_engine_dispatches_and_marks_cooldown():
    path = make_config_file({
        "tok": {
            "voipToken": "tok",
            "cameras": {
                "front_door": {"enabled": True, "minScore": 0.5, "cooldownSeconds": 60, "requiredObjects": ["person"]}
            },
        }
    })
    config = ConfigStore(path=path)
    cooldown = CooldownStore()
    dispatcher = MagicMock(spec=VoIPDispatcher)
    dispatcher.dispatch.return_value = True
    engine = TriggerEngine(config=config, dispatcher=dispatcher, cooldown=cooldown)

    event = {"camera": "front_door", "label": "person", "top_score": 0.9, "current_zones": [], "id": "evt-1"}

    # First event dispatches
    assert engine.handle_event(event) == 1
    assert dispatcher.dispatch.call_count == 1

    # Second event within cooldown is suppressed
    assert engine.handle_event(event) == 0
    assert dispatcher.dispatch.call_count == 1

    os.unlink(path)


def test_voip_dispatcher_payload_shape():
    """Smoke test that headers + body are constructed correctly."""
    # Mock httpx by monkeypatching the Client.post on the instance.
    captured = {}

    class FakeResp:
        status_code = 200
        text = ""

    def fake_post(self, url, headers=None, content=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["content"] = content
        return FakeResp()

    import httpx
    orig = httpx.Client.post
    httpx.Client.post = fake_post  # type: ignore
    try:
        dispatcher = VoIPDispatcher(jwt_provider=lambda: "FAKE.JWT")
        ok = dispatcher.dispatch(
            device_token_hex="abc123",
            camera_id="front_door",
            camera_display_name="Front Door",
            snapshot_url="http://x/snap.jpg",
            event_id="evt-1",
        )
        assert ok is True
        assert captured["url"].endswith("/3/device/abc123")
        assert captured["headers"]["apns-push-type"] == "voip"
        assert captured["headers"]["apns-priority"] == "10"
        assert captured["headers"]["apns-topic"].endswith(".voip")
        body = json.loads(captured["content"])
        assert body["cameraId"] == "front_door"
        assert body["snapshotURL"] == "http://x/snap.jpg"
        assert body["eventId"] == "evt-1"
        assert body["aps"]["content-available"] == 1
    finally:
        httpx.Client.post = orig  # type: ignore


# --- Phase 2.C: REST sync endpoint -----------------------------------------

import asyncio
from aiohttp import web
from aiohttp.test_utils import TestServer, TestClient
from src.auto_call.sync_endpoint import make_app, _atomic_write_json, _load_existing


def test_atomic_write_and_load(tmp_path):
    path = tmp_path / "configs.json"
    _atomic_write_json(str(path), {"tok": {"cameras": {}}})
    assert _load_existing(str(path)) == {"tok": {"cameras": {}}}


def test_atomic_write_overwrites(tmp_path):
    path = tmp_path / "configs.json"
    _atomic_write_json(str(path), {"v1": 1})
    _atomic_write_json(str(path), {"v2": 2})
    assert _load_existing(str(path)) == {"v2": 2}


@pytest.mark.asyncio
async def test_sync_endpoint_writes_payload(tmp_path, monkeypatch):
    cfg_path = tmp_path / "configs.json"
    monkeypatch.setenv("LUMEN_AUTOCALL_CONFIG_PATH", str(cfg_path))
    # Re-import the module so it picks up the env var.
    import importlib
    import src.auto_call.sync_endpoint as m
    importlib.reload(m)

    app = m.make_app()
    async with TestClient(TestServer(app)) as client:
        body = {
            "voipToken": "deadbeef",
            "cameras": {"front_door": {"enabled": True}},
            "serverURL": "http://10.0.0.1:5000",
        }
        resp = await client.post("/auto-call/sync", json=body)
        assert resp.status == 200
        json_resp = await resp.json()
        assert json_resp["ok"] is True

    saved = json.loads(cfg_path.read_text())
    assert "deadbeef" in saved
    assert saved["deadbeef"]["serverURL"] == "http://10.0.0.1:5000"


@pytest.mark.asyncio
async def test_sync_endpoint_rejects_missing_voipToken(tmp_path, monkeypatch):
    monkeypatch.setenv("LUMEN_AUTOCALL_CONFIG_PATH", str(tmp_path / "x.json"))
    import importlib
    import src.auto_call.sync_endpoint as m
    importlib.reload(m)
    async with TestClient(TestServer(m.make_app())) as client:
        resp = await client.post("/auto-call/sync", json={"cameras": {}})
        assert resp.status == 400


@pytest.mark.asyncio
async def test_health_endpoint(tmp_path):
    async with TestClient(TestServer(make_app())) as client:
        resp = await client.get("/auto-call/health")
        assert resp.status == 200
