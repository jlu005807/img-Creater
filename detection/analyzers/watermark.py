"""Invisible-watermark analyzer (high-confidence rule).

Tries to decode the watermark that Stable Diffusion (and compatible tools)
embed via the ``invisible-watermark`` library. SD writes the ASCII marker
``"StableDiffusionV1"`` as a *bytes* watermark, recovered with
``WatermarkDecoder("bytes", 136)`` (17 bytes x 8 = 136 bits).

A decode is treated as a HIT only when the recovered bytes EXACTLY match a
known marker. ``invisible-watermark`` always returns a fixed-length payload —
it never reports "no watermark present" — so on an unwatermarked image the
payload is effectively random. Requiring an exact marker match is what keeps
this high-confidence stage (which short-circuits the whole detector) from
firing on ordinary photos. Needs numpy + Pillow + imwatermark.
"""

from __future__ import annotations

import io
from typing import Any

# Known ASCII markers embedded by AI image tools. The decoder length (in bits)
# is derived from each marker's byte length, so only an image actually carrying
# one of these exact strings yields a hit.
_DEFAULT_MARKERS = (b"StableDiffusionV1",)


def analyze(image_bytes: bytes, cfg: dict[str, Any]) -> dict[str, Any]:
    try:
        import numpy as np
        from PIL import Image
        from imwatermark import WatermarkDecoder
    except ImportError:
        # Optional dep missing — no signal, excluded from fusion.
        return {"score": None, "hit": False, "signals": {}, "evidence": [], "error": "invisible-watermark 未安装"}

    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        # imwatermark expects BGR (OpenCV-style) uint8 arrays.
        rgb = np.array(img)
        bgr = rgb[:, :, ::-1].copy()
    except Exception as exc:  # noqa: BLE001
        return {"score": None, "hit": False, "signals": {}, "evidence": [], "error": str(exc)}

    if bgr.shape[0] < 256 or bgr.shape[1] < 256:
        # The DWT-DCT watermark needs enough resolution to decode reliably.
        return {"score": 0.0, "hit": False, "signals": {"too_small": True}, "evidence": [], "error": None}

    for marker in _markers(cfg):
        length_bits = len(marker) * 8
        try:
            decoder = WatermarkDecoder("bytes", length_bits)
            payload = decoder.decode(bgr, "dwtDct")
        except Exception:  # noqa: BLE001 - try the next marker
            continue
        raw = bytes(payload) if payload is not None else b""
        if raw == marker:
            text = marker.decode("utf-8", errors="replace")
            return {
                "score": 1.0,
                "hit": True,
                "signals": {"wm_type": "bytes", "length": length_bits, "marker": text},
                "evidence": [f"命中隐形水印: {text}"],
                "error": None,
            }

    return {"score": 0.0, "hit": False, "signals": {"decoded": False}, "evidence": [], "error": None}


def _markers(cfg: dict[str, Any]) -> list[bytes]:
    """Known markers, optionally extended via ``cfg['watermark']['markers']``."""
    out = list(_DEFAULT_MARKERS)
    extra = (cfg.get("watermark") or {}).get("markers", [])
    for m in extra:
        if isinstance(m, str):
            out.append(m.encode("utf-8", "ignore"))
        elif isinstance(m, (bytes, bytearray)):
            out.append(bytes(m))
    return out
