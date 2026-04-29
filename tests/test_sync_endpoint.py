"""HTTP endpoint integration tests."""
import json
import asyncio

import pytest
from aiohttp.test_utils import TestClient, TestServer

from src.auto_call.discovery.cache import DiscoveryCache
from src.auto_call.sync_endpoint import make_app


@pytest.mark.asyncio
async def test_sync_writes_buttons(tmp_path, monkeypatch):
    cfg_path = tmp_path / "cfg.json"
    monkeypatch.setenv("LUMEN_AUTOCALL_CONFIG_PATH", str(cfg_path))
    # Re-import module so it picks up the monkey-patched env var.
    import importlib, src.auto_call.sync_endpoint as se
    importlib.reload(se)

    queue: asyncio.Queue = asyncio.Queue(maxsize=4)
    app = se.make_app(subs_reconcile_queue=queue)
    async with TestServer(app) as server, TestClient(server) as client:
        body = {
            "voipToken": "tok1",
            "cameras": {"front": {"enabled": True}},
            "buttons": [{"id": "u1", "name": "B", "topic": "t",
                         "match": {"pattern": {"type": "any"}},
                         "cameras": ["front"], "enabled": True}],
        }
        resp = await client.post("/auto-call/sync", json=body)
        assert resp.status == 200
        payload = await resp.json()
        assert payload["buttons"] == 1

    on_disk = json.loads(cfg_path.read_text())
    assert on_disk["tok1"]["buttons"][0]["topic"] == "t"
    # Reconcile signal queued for the main loop.
    assert not queue.empty()


@pytest.mark.asyncio
async def test_discover_returns_cached_entities():
    cache = DiscoveryCache()
    cache.update("homeassistant/button/btn1/config",
                 json.dumps({"name": "Btn1", "command_topic": "x"}).encode())
    app = make_app(discovery_cache=cache)
    async with TestServer(app) as server, TestClient(server) as client:
        resp = await client.get("/auto-call/discover")
        assert resp.status == 200
        data = await resp.json()
        assert len(data) == 1
        assert data[0]["name"] == "Btn1"
        assert data[0]["topic"] == "x"


@pytest.mark.asyncio
async def test_discover_returns_empty_when_cache_absent():
    app = make_app(discovery_cache=None)
    async with TestServer(app) as server, TestClient(server) as client:
        resp = await client.get("/auto-call/discover")
        assert resp.status == 200
        assert await resp.json() == []


@pytest.mark.asyncio
async def test_sync_rejects_non_array_buttons():
    app = make_app()
    async with TestServer(app) as server, TestClient(server) as client:
        resp = await client.post("/auto-call/sync", json={
            "voipToken": "tok1", "cameras": {}, "buttons": "not-array",
        })
        assert resp.status == 400


@pytest.mark.asyncio
async def test_sync_accepts_missing_buttons_field(tmp_path, monkeypatch):
    cfg_path = tmp_path / "cfg.json"
    monkeypatch.setenv("LUMEN_AUTOCALL_CONFIG_PATH", str(cfg_path))
    import importlib, src.auto_call.sync_endpoint as se
    importlib.reload(se)

    app = se.make_app()
    async with TestServer(app) as server, TestClient(server) as client:
        resp = await client.post("/auto-call/sync", json={
            "voipToken": "tok1", "cameras": {},
        })
        assert resp.status == 200
        payload = await resp.json()
        assert payload["buttons"] == 0
