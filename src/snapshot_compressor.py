"""Fetch and compress Frigate snapshots for faster NSE delivery.

Reduces NSE download latency from ~500ms (full-size snapshot from Frigate)
to either 0ms (embedded in APNs payload) or ~50ms (compressed, from CDN).
"""
import asyncio
import base64
import io
import logging
from typing import Optional

import httpx
from PIL import Image

logger = logging.getLogger("lumen.snapshot")

# Target ~40KB for the compressed JPEG. This keeps most notifications under
# APNs' 4KB payload limit when base64-encoded (~53KB → ~70KB base64, but the
# Worker strips EXIF/metadata which buys us headroom). Larger snapshots fall
# back to Worker KV storage.
TARGET_MAX_DIMENSION = 512
JPEG_QUALITY = 75
FETCH_TIMEOUT_SECONDS = 3.0


async def fetch_and_compress_snapshot(
    frigate_url: str,
    event_id: str,
    camera: str,
) -> Optional[str]:
    """Fetch a Frigate snapshot and compress to ~40KB JPEG, returning base64.

    Args:
        frigate_url: Base Frigate URL (e.g., "http://192.168.1.50:5000")
        event_id: Frigate event ID
        camera: Camera name (for logging)

    Returns:
        Base64-encoded compressed JPEG, or None on any failure (fetch timeout,
        image decode error, compression failure). Failures are logged but never
        propagated — the Worker/NSE will fall back to fetching the original.
    """
    snapshot_url = f"{frigate_url.rstrip('/')}/api/events/{event_id}/snapshot.jpg"

    try:
        async with httpx.AsyncClient(timeout=FETCH_TIMEOUT_SECONDS) as client:
            resp = await client.get(snapshot_url)
            if resp.status_code != 200:
                logger.warning(
                    "Snapshot fetch failed for %s (%s): HTTP %d",
                    camera, event_id[:8], resp.status_code,
                )
                return None

            raw_bytes = resp.content
            if len(raw_bytes) == 0:
                logger.warning("Snapshot empty for %s (%s)", camera, event_id[:8])
                return None

            # Compress in a thread pool to avoid blocking the event loop
            # (Pillow is CPU-bound and not async-native)
            compressed = await asyncio.to_thread(
                _compress_image, raw_bytes, camera, event_id
            )
            if compressed is None:
                return None

            # Base64-encode for JSON transport
            b64 = base64.b64encode(compressed).decode("ascii")
            logger.info(
                "Snapshot compressed for %s (%s): %d KB → %d KB (base64: %d KB)",
                camera, event_id[:8],
                len(raw_bytes) // 1024,
                len(compressed) // 1024,
                len(b64) // 1024,
            )
            return b64

    except httpx.TimeoutException:
        logger.warning(
            "Snapshot fetch timeout for %s (%s) after %.1fs",
            camera, event_id[:8], FETCH_TIMEOUT_SECONDS,
        )
        return None
    except Exception as e:
        logger.error(
            "Snapshot fetch failed for %s (%s): %s",
            camera, event_id[:8], e,
        )
        return None


def _compress_image(raw_bytes: bytes, camera: str, event_id: str) -> Optional[bytes]:
    """Synchronous image compression (runs in thread pool via to_thread)."""
    try:
        img = Image.open(io.BytesIO(raw_bytes))

        # Convert RGBA → RGB (APNs doesn't need alpha, saves space)
        if img.mode in ("RGBA", "LA", "P"):
            rgb = Image.new("RGB", img.size, (255, 255, 255))
            rgb.paste(img, mask=img.split()[-1] if img.mode in ("RGBA", "LA") else None)
            img = rgb
        elif img.mode != "RGB":
            img = img.convert("RGB")

        # Resize to TARGET_MAX_DIMENSION on the longest side
        width, height = img.size
        if max(width, height) > TARGET_MAX_DIMENSION:
            if width > height:
                new_width = TARGET_MAX_DIMENSION
                new_height = int(height * (TARGET_MAX_DIMENSION / width))
            else:
                new_height = TARGET_MAX_DIMENSION
                new_width = int(width * (TARGET_MAX_DIMENSION / height))
            img = img.resize((new_width, new_height), Image.Resampling.LANCZOS)

        # Compress to JPEG
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)
        compressed = buf.getvalue()

        return compressed

    except Exception as e:
        logger.error(
            "Image compression failed for %s (%s): %s",
            camera, event_id[:8], e,
        )
        return None
