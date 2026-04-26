"""HTTP endpoint that the iOS app POSTs auto-call configs to.

Atomically writes to the file backing ConfigStore.

Usage from relay.py main loop:

    from src.auto_call.sync_endpoint import start_sync_server
    runner = await start_sync_server(host="0.0.0.0", port=8765)
    # ... run forever
    await runner.cleanup()
"""
import json
import logging
import os
import tempfile
from typing import Any, Dict

from aiohttp import web

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

    server_url = body.get("serverURL")
    auth_header = body.get("authHeader")

    existing = _load_existing(CONFIG_PATH)
    existing[voip_token] = {
        "voipToken": voip_token,
        "serverURL": server_url,
        "authHeader": auth_header,
        "cameras": cameras,
    }
    try:
        _atomic_write_json(CONFIG_PATH, existing)
    except Exception as e:
        logger.error("atomic write failed: %s", e)
        return web.json_response({"error": "write_failed"}, status=500)

    logger.info(
        "auto_call sync: token=%s... cameras=%s server=%s",
        voip_token[:10],
        list(cameras.keys()),
        server_url,
    )
    return web.json_response({"ok": True, "cameras": list(cameras.keys())})


async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok", "service": "lumen-auto-call-sync"})


async def handle_test_push(request: web.Request) -> web.Response:
    """Dispatch a single fake VoIP push to a specified token. Debug only.

    Expects POST body:
        {"voipToken": "<hex>",
         "cameraId": "test_camera",          # optional
         "displayName": "Test Caller"}        # optional
    """
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "invalid_json"}, status=400)

    voip_token = body.get("voipToken")
    if not isinstance(voip_token, str) or not voip_token:
        return web.json_response({"ok": False, "error": "missing_voip_token"}, status=400)

    camera_id = body.get("cameraId", "test_camera")
    display_name = body.get("displayName", "Test Caller")

    # Lazy import to avoid circular references / import-time env-var requirements.
    from .apns_jwt import APNsJWTSigner
    from .voip_dispatcher import VoIPDispatcher

    try:
        signer = APNsJWTSigner.from_env()
    except (KeyError, OSError, FileNotFoundError) as e:
        return web.json_response(
            {"ok": False, "error": f"apns_signer_init_failed: {e}"},
            status=500,
        )

    dispatcher = VoIPDispatcher(jwt_provider=signer.token)
    try:
        ok = await dispatcher.dispatch(
            device_token_hex=voip_token,
            camera_id=camera_id,
            camera_display_name=display_name,
            snapshot_url=None,
            event_id="test-event",
            trigger_type="test",
        )
    finally:
        await dispatcher.aclose()

    return web.json_response({"ok": ok, "dispatched": ok})


def make_app() -> web.Application:
    app = web.Application()
    app.router.add_post("/auto-call/sync", handle_sync)
    app.router.add_post("/auto-call/test-push", handle_test_push)
    app.router.add_get("/auto-call/health", handle_health)
    return app


async def start_sync_server(host: str = "0.0.0.0", port: int = 8765) -> web.AppRunner:
    app = make_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    logger.info("auto_call sync server listening on %s:%d", host, port)
    return runner
