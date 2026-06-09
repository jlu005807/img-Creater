from __future__ import annotations

import json
import shutil
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
            quality=payload.get("quality"),
            reference_images=payload.get("reference_images"),
            history_id=payload.get("history_id"),
        )
        # 任务进入后台 worker（OpenAI 兼容调用或异步中转轮询），尚未生成完成，返回 202 Accepted。
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
            quality=payload.get("quality"),
            composite=payload.get("composite"),
            history_id=payload.get("history_id"),
        )
        # 编辑任务与文生图一样走后台 worker，前端继续用 /status 轮询同一个 task_id。
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


@generation_bp.post("/tasks/<task_id>/cancel")
def cancel_generation_task(task_id: str):
    try:
        return _success(_image_service().cancel_generation(task_id=task_id))
    except Exception as exc:
        return _image_error(exc)


@generation_bp.get("/sessions")
def list_sessions():
    try:
        result_dir = _result_dir()
        sessions = []
        if not result_dir.exists():
            return _success([])
        for session_dir in result_dir.iterdir():
            if not session_dir.is_dir():
                continue
            manifest_path = session_dir / "session.json"
            if not manifest_path.exists():
                continue
            with manifest_path.open("r", encoding="utf-8-sig") as fh:
                manifest = json.load(fh)
            if not isinstance(manifest, dict) or manifest.get("status") != "completed":
                continue
            urls = [url for url in manifest.get("urls", []) if isinstance(url, str)]
            if not urls:
                continue
            item = dict(manifest)
            item["images"] = [
                {
                    "url": url,
                    "filename": url.rsplit("/", 1)[-1],
                    "session_id": item.get("id") or session_dir.name,
                }
                for url in urls
            ]
            sessions.append(item)
        sessions.sort(key=lambda item: item.get("updated_at") or item.get("created_at") or "", reverse=True)
        return _success(sessions)
    except Exception as exc:
        return _image_error(exc)


@generation_bp.delete("/sessions/<history_id>")
def delete_session(history_id: str):
    try:
        session_dir = _result_dir() / _normalize_history_id(history_id)
        if session_dir.exists():
            shutil.rmtree(session_dir)
        return _success({"deleted": True, "id": _normalize_history_id(history_id)})
    except Exception as exc:
        return _image_error(exc)


@generation_bp.delete("/sessions")
def delete_sessions():
    try:
        result_dir = _result_dir()
        deleted = 0
        if result_dir.exists():
            for session_dir in result_dir.iterdir():
                if session_dir.is_dir():
                    shutil.rmtree(session_dir)
                    deleted += 1
        return _success({"deleted": deleted})
    except Exception as exc:
        return _image_error(exc)


@generation_bp.get("/edit-drafts/<history_id>")
def get_edit_draft(history_id: str):
    try:
        draft_path = _edit_draft_path(history_id)
        if not draft_path.exists():
            return _success(None)
        with draft_path.open("r", encoding="utf-8-sig") as fh:
            return _success(json.load(fh))
    except Exception as exc:
        return _image_error(exc)


@generation_bp.put("/edit-drafts/<history_id>")
def save_edit_draft(history_id: str):
    try:
        draft_path = _edit_draft_path(history_id)
        payload = _json_payload()
        if not payload:
            raise ImageServiceError("局部编辑草稿不能为空", status_code=400)
        draft_path.parent.mkdir(parents=True, exist_ok=True)
        with draft_path.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        return _success({"history_id": _normalize_history_id(history_id)})
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


def _result_dir():
    service = _image_service()
    result_dir = getattr(service, "result_dir", None)
    if result_dir is None:
        from backend.services.image_service import DEFAULT_RESULT_DIR

        result_dir = DEFAULT_RESULT_DIR
    return result_dir


def _normalize_history_id(value: Any) -> str:
    normalized = ImageService._normalize_history_id(value)
    if not normalized:
        raise ImageServiceError("history_id 不能为空", status_code=400)
    return normalized


def _edit_draft_path(history_id: str):
    return _result_dir() / _normalize_history_id(history_id) / "edit-draft.json"


def _success(data: Any, status: int = 200):
    return jsonify({"success": True, "data": data}), status


def _image_error(exc: Exception):
    if isinstance(exc, ImageServiceError):
        return _error(exc.message, exc.status_code, exc.details)
    current_app.logger.exception("Unhandled generation route error")
    return _error("服务器内部错误", 500)


def _error(message: str, status: int, details: dict[str, Any] | None = None):
    return jsonify({"success": False, "error": {"message": message, "details": details or {}}}), status
