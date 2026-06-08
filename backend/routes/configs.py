from __future__ import annotations

from typing import Any

from flask import Blueprint, current_app, jsonify, request

from backend.services.config_service import (
    ConfigNotFoundError,
    ConfigService,
    ConfigServiceError,
    ConfigValidationError,
)


configs_bp = Blueprint("configs", __name__)


@configs_bp.get("")
@configs_bp.get("/")
def list_configs():
    try:
        configs = _config_service().list_configs()
        return _success([ConfigService.public_config(item) for item in configs])
    except Exception as exc:
        return _config_error(exc)


@configs_bp.post("")
@configs_bp.post("/")
def create_config():
    try:
        payload = _json_payload()
        config = _config_service().create_config(payload)
        return _success(ConfigService.public_config(config), status=201)
    except Exception as exc:
        return _config_error(exc)


@configs_bp.put("/<config_id>")
def update_config(config_id: str):
    try:
        payload = _json_payload()
        config = _config_service().update_config(config_id, payload)
        return _success(ConfigService.public_config(config))
    except Exception as exc:
        return _config_error(exc)


@configs_bp.get("/<config_id>/secret")
def get_config_secret(config_id: str):
    try:
        return _success(_config_service().get_secret(config_id))
    except Exception as exc:
        return _config_error(exc)


@configs_bp.delete("/<config_id>")
def delete_config(config_id: str):
    try:
        _config_service().delete_config(config_id)
        return _success({"deleted": True, "id": config_id})
    except Exception as exc:
        return _config_error(exc)


@configs_bp.post("/reorder")
def reorder_configs():
    try:
        payload = _json_payload()
        ordered_ids = payload.get("ordered_ids")
        if not isinstance(ordered_ids, list) or not all(isinstance(item, str) for item in ordered_ids):
            raise ConfigValidationError("ordered_ids 必须是字符串数组")
        configs = _config_service().reorder_configs(ordered_ids)
        return _success([ConfigService.public_config(item) for item in configs])
    except Exception as exc:
        return _config_error(exc)


def _config_service() -> ConfigService:
    service = current_app.config.get("CONFIG_SERVICE")
    if service is not None:
        return service
    return ConfigService()


def _json_payload() -> dict[str, Any]:
    payload = request.get_json(silent=True)
    if payload is None:
        return {}
    if not isinstance(payload, dict):
        raise ConfigValidationError("请求体必须是 JSON 对象")
    return payload


def _success(data: Any, status: int = 200):
    return jsonify({"success": True, "data": data}), status


def _config_error(exc: Exception):
    if isinstance(exc, ConfigNotFoundError):
        return _error(str(exc).strip("'"), 404)
    if isinstance(exc, ConfigValidationError):
        return _error(str(exc), 400)
    if isinstance(exc, ConfigServiceError):
        return _error(str(exc), 500)
    current_app.logger.exception("Unhandled config route error")
    return _error("服务器内部错误", 500)


def _error(message: str, status: int, details: dict[str, Any] | None = None):
    return jsonify({"success": False, "error": {"message": message, "details": details or {}}}), status
