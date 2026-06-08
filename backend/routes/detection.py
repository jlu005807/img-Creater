"""Thin HTTP adapter for the decoupled detection module (beta).

This is the only place the host app touches `detection`. It imports it lazily
and degrades gracefully if the module or its deps are absent, so the rest of
the app is unaffected. Mirrors the {success, data} / {success, error} envelope
used by the other routes.
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path
from typing import Any

from flask import Blueprint, current_app, jsonify, request


detection_bp = Blueprint("detection", __name__)

# detection/ lives at the repo root (sibling of backend/), not under backend.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


@detection_bp.get("/health")
def detect_health():
    try:
        from detection import detector_health

        return _success(detector_health())
    except Exception as exc:  # noqa: BLE001 - never break the app over the beta module
        current_app.logger.warning("detection health probe failed: %s", exc)
        return _success({"available": False, "missing_required": ["detection 模块不可用"], "error": str(exc)})


@detection_bp.post("")
@detection_bp.post("/")
def detect():
    try:
        from detection import detect_image
    except Exception as exc:  # noqa: BLE001
        return _error("检测模块未启用（缺少依赖）", 503, {"error": str(exc)})

    payload = request.get_json(silent=True) or {}
    image_field = payload.get("image", "")
    image_bytes, err = _decode_image(image_field)
    if err:
        return _error(err, 400)

    try:
        result = detect_image(image_bytes, filename=str(payload.get("filename", "")))
    except Exception as exc:  # noqa: BLE001 - the module shouldn't raise, but be safe
        current_app.logger.exception("detection failed")
        return _error("检测执行失败", 500, {"error": str(exc)})
    return _success(result)


def _decode_image(field: Any) -> tuple[bytes, str | None]:
    text = str(field or "").strip()
    if not text:
        return b"", "缺少 image 字段"
    if text.startswith("data:"):
        if ";base64," not in text:
            return b"", "image 必须是 data:*;base64,... 格式"
        text = text.split(";base64,", 1)[1]
    try:
        return base64.b64decode(text), None
    except Exception:  # noqa: BLE001
        return b"", "image base64 解码失败"


def _success(data: Any, status: int = 200):
    return jsonify({"success": True, "data": data}), status


def _error(message: str, status: int, details: dict[str, Any] | None = None):
    return jsonify({"success": False, "error": {"message": message, "details": details or {}}}), status
