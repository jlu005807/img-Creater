from __future__ import annotations

from typing import Any

from flask import Blueprint, current_app, jsonify, request

from backend.services.config_service import ConfigService
from backend.services.image_service import ImageService, ImageServiceError


generation_bp = Blueprint("generation", __name__)


@generation_bp.post("/generate")
def generate_image():
    try:
        payload = _json_payload()
        result = _image_service().submit_generation(
            prompt=payload.get("prompt", ""),
            size=payload.get("size", "1024x1024"),
            n=payload.get("n", 1),
        )
        # 中转站提交成功只代表异步任务已入队，图片未生成完成，因此返回 202 Accepted。
        return _success(result, status=202)
    except Exception as exc:
        return _image_error(exc)


@generation_bp.post("/edit")
def edit_image():
    try:
        payload = _json_payload()
        result = _image_service().submit_edit_generation(
            prompt=payload.get("prompt", ""),
            image=payload.get("image", ""),
            mask=payload.get("mask", ""),
            size=payload.get("size", "1024x1024"),
            n=payload.get("n", 1),
            edit_mode=payload.get("edit_mode", "mask"),
            selection=payload.get("selection"),
        )
        # 编辑任务与文生图一样走异步队列，前端继续用 /status 轮询同一个 task_id。
        return _success(result, status=202)
    except Exception as exc:
        return _image_error(exc)


@generation_bp.get("/status")
def get_generation_status():
    try:
        api_id = request.args.get("api_id", "")
        task_id = request.args.get("task_id", "")
        return _success(_image_service().poll_generation_status(api_id=api_id, task_id=task_id))
    except Exception as exc:
        return _image_error(exc)


def _image_service() -> ImageService:
    service = current_app.config.get("IMAGE_SERVICE")
    if service is not None:
        return service

    config_service = current_app.config.get("CONFIG_SERVICE")
    if config_service is None:
        config_service = ConfigService()
    return ImageService(config_service=config_service)


def _json_payload() -> dict[str, Any]:
    payload = request.get_json(silent=True)
    if payload is None:
        return {}
    if not isinstance(payload, dict):
        raise ImageServiceError("请求体必须是 JSON 对象", status_code=400)
    return payload


def _success(data: Any, status: int = 200):
    return jsonify({"success": True, "data": data}), status


def _image_error(exc: Exception):
    if isinstance(exc, ImageServiceError):
        return _error(exc.message, exc.status_code, exc.details)
    current_app.logger.exception("Unhandled generation route error")
    return _error("服务器内部错误", 500)


def _error(message: str, status: int, details: dict[str, Any] | None = None):
    return jsonify({"success": False, "error": {"message": message, "details": details or {}}}), status
