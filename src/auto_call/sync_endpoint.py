"""HTTP endpoint that the iOS app POSTs auto-call configs to.

Endpoints:
  POST /auto-call/sync       — full upsert of one device's configs + buttons
  POST /auto-call/test-push  — debug VoIP push for editor "Test" button
  GET  /auto-call/discover   — list HA-discovered button-shaped entities
  GET  /auto-call/health     — health probe
"""
import asyncio
import json
import logging
import os
import tempfile
from typing import Any, Dict, Optional

from aiohttp import web

from .discovery.cache import DiscoveryCache

logger = logging.getLogger("lumen.auto_call.sync")

CONFIG_PATH = os.environ.get(
    "LUMEN_AUTOCALL_CONFIG_PATH",
    "/var/lib/lumen-push-relay/auto_call_configs.json",
)


def _atomic_write_json(path: str, data: Dict[str, Any]) -> None:
    parent = os.path.dirname(path) or "."
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".auto_call_", suffix=".json", dir=parent)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def _load_existing(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


async def handle_sync(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)

    voip_token = body.get("voipToken")
    cameras = body.get("cameras")
    if not isinstance(voip_token, str) or not voip_token:
        return web.json_response({"error": "missing_voip_token"}, status=400)
    if not isinstance(cameras, dict):
        return web.json_response({"error": "missing_cameras"}, status=400)

    # buttons is optional; default to empty list for back-compat with v1 clients.
    buttons = body.get("buttons")
    if buttons is None:
        buttons = []
    elif not isinstance(buttons, list):
        return web.json_response({"error": "buttons_must_be_array"}, status=400)

    server_url = body.get("serverURL")
    auth_header = body.get("authHeader")

    existing = _load_existing(CONFIG_PATH)
    existing[voip_token] = {
        "voipToken": voip_token,
        "serverURL": server_url,
        "authHeader": auth_header,
        "cameras": cameras,
        "buttons": buttons,
    }
    try:
        _atomic_write_json(CONFIG_PATH, existing)
    except Exception as e:
        logger.error("atomic write failed: %s", e)
        return web.json_response({"error": "write_failed"}, status=500)

    # Schedule a reconcile pass so SubscriptionManager picks up new/removed
    # button topics within milliseconds of the save (don't wait for mtime poll).
    queue: Optional[asyncio.Queue] = request.app.get("subs_reconcile_queue")
    if queue is not None:
        try:
            queue.put_nowait("reload")
        except asyncio.QueueFull:
            pass  # already-pending reconcile is sufficient

    logger.info(
        "auto_call sync: token=%s... cameras=%s buttons=%d server=%s",
        voip_token[:10], list(cameras.keys()), len(buttons), server_url,
    )
    return web.json_response({
        "ok": True,
        "cameras": list(cameras.keys()),
        "buttons": len(buttons),
    })


async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok", "service": "lumen-auto-call-sync"})


async def handle_test_push(request: web.Request) -> web.Response:
    """Dispatch a single fake VoIP push to a specified token. Debug only."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "invalid_json"}, status=400)

    voip_token = body.get("voipToken")
    if not isinstance(voip_token, str) or not voip_token:
        return web.json_response({"ok": False, "error": "missing_voip_token"}, status=400)

    camera_id = body.get("cameraId", "test_camera")
    display_name = body.get("displayName", "Test Caller")

    from .apns_jwt import APNsJWTSigner
    from .voip_dispatcher import VoIPDispatcher

    try:
        signer = APNsJWTSigner.from_env()
    except (KeyError, OSError, FileNotFoundError) as e:
        return web.json_response(
            {"ok": False, "error": f"apns_signer_init_failed: {e}"}, status=500,
        )

    dispatcher = VoIPDispatcher(jwt_provider=signer.token)
    try:
        ok = await dispatcher.dispatch(
            device_token_hex=voip_token, camera_id=camera_id,
            camera_display_name=display_name, snapshot_url=None,
            event_id="test-event", trigger_type="test",
        )
    finally:
        await dispatcher.aclose()

    return web.json_response({"ok": ok, "dispatched": ok})


async def handle_discover(request: web.Request) -> web.Response:
    """List HA-discovered button-shaped entities currently in the relay's cache."""
    cache: Optional[DiscoveryCache] = request.app.get("discovery_cache")
    if cache is None:
        return web.json_response([], status=200)
    return web.json_response([
        {
            "objectId": e.object_id,
            "name": e.name,
            "topic": e.topic,
            "deviceClass": e.device_class,
            "suggestedMatch": e.suggested_match,
        }
        for e in cache.list()
    ])


def make_app(
    discovery_cache: Optional[DiscoveryCache] = None,
    subs_reconcile_queue: Optional[asyncio.Queue] = None,
) -> web.Application:
    app = web.Application()
    app["discovery_cache"] = discovery_cache
    app["subs_reconcile_queue"] = subs_reconcile_queue
    app.router.add_post("/auto-call/sync", handle_sync)
    app.router.add_post("/auto-call/test-push", handle_test_push)
    app.router.add_get("/auto-call/discover", handle_discover)
    app.router.add_get("/auto-call/health", handle_health)
    return app


async def start_sync_server(
    host: str = "0.0.0.0", port: int = 8765,
    discovery_cache: Optional[DiscoveryCache] = None,
    subs_reconcile_queue: Optional[asyncio.Queue] = None,
) -> web.AppRunner:
    app = make_app(discovery_cache=discovery_cache, subs_reconcile_queue=subs_reconcile_queue)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    logger.info("auto_call sync server listening on %s:%d (discovery=%s, queue=%s)",
                host, port, discovery_cache is not None, subs_reconcile_queue is not None)
    return runner
