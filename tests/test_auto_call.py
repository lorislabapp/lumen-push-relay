"""Tests for the auto-call subsystem."""
import json
import os
import tempfile
import time

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


async def test_trigger_engine_dispatches_and_marks_cooldown():
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

    class FakeDispatcher:
        def __init__(self):
            self.calls = 0

        async def dispatch(self, **kwargs):
            self.calls += 1
            return True

    dispatcher = FakeDispatcher()
    engine = TriggerEngine(config=config, dispatcher=dispatcher, cooldown=cooldown)

    event = {"camera": "front_door", "label": "person", "top_score": 0.9, "current_zones": [], "id": "evt-1"}

    # First event dispatches
    assert await engine.handle_event(event) == 1
    assert dispatcher.calls == 1

    # Second event within cooldown is suppressed
    assert await engine.handle_event(event) == 0
    assert dispatcher.calls == 1

    os.unlink(path)


async def test_voip_dispatcher_payload_shape():
    """Smoke test that headers + body are constructed correctly."""
    # Mock httpx.AsyncClient.post by monkeypatching the class method.
    captured = {}

    class FakeResp:
        status_code = 200
        text = ""

    async def fake_post(self, url, headers=None, content=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["content"] = content
        return FakeResp()

    import httpx
    orig = httpx.AsyncClient.post
    httpx.AsyncClient.post = fake_post  # type: ignore
    dispatcher = VoIPDispatcher(jwt_provider=lambda: "FAKE.JWT")
    try:
        ok = await dispatcher.dispatch(
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
        httpx.AsyncClient.post = orig  # type: ignore
        await dispatcher.aclose()


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


@pytest.mark.asyncio
async def test_test_push_endpoint_rejects_missing_token(tmp_path, monkeypatch):
    monkeypatch.setenv("LUMEN_AUTOCALL_CONFIG_PATH", str(tmp_path / "x.json"))
    import importlib, src.auto_call.sync_endpoint as m
    importlib.reload(m)
    async with TestClient(TestServer(m.make_app())) as client:
        resp = await client.post("/auto-call/test-push", json={})
        assert resp.status == 400
        body = await resp.json()
        assert body["ok"] is False
        assert "missing_voip_token" in body["error"]


@pytest.mark.asyncio
async def test_test_push_endpoint_returns_500_when_signer_unconfigured(tmp_path, monkeypatch):
    monkeypatch.setenv("LUMEN_AUTOCALL_CONFIG_PATH", str(tmp_path / "x.json"))
    # Remove any APNS env vars so signer init fails cleanly.
    for var in ["APNS_TEAM_ID", "APNS_KEY_ID", "APNS_KEY_FILE", "APNS_KEY_PATH"]:
        monkeypatch.delenv(var, raising=False)
    import importlib, src.auto_call.sync_endpoint as m
    importlib.reload(m)
    async with TestClient(TestServer(m.make_app())) as client:
        resp = await client.post("/auto-call/test-push", json={"voipToken": "abc"})
        assert resp.status == 500
        body = await resp.json()
        assert body["ok"] is False
        assert "apns_signer_init_failed" in body["error"]


# --- Phase 2.D: shared JWT signer ------------------------------------------


def test_apns_jwt_signer_caches():
    """Signer caches a token for the cache_seconds window, then refreshes."""
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives import serialization

    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()

    from src.auto_call.apns_jwt import APNsJWTSigner
    signer = APNsJWTSigner(team_id="TEAM", key_id="KID", private_key_pem=pem, cache_seconds=10)
    t1 = signer.token()
    t2 = signer.token()
    assert t1 == t2  # cache hit
    # Force expiry; token should regenerate (can't assert inequality because
    # iat at second granularity may match if called in the same second).
    signer._cached_at = 0
    t3 = signer.token()
    assert t3
