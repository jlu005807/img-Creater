from __future__ import annotations

from typing import Any

from flask import Blueprint, current_app, jsonify, request

from backend.services.prompt_template_service import (
    PromptTemplateNotFoundError,
    PromptTemplateService,
    PromptTemplateServiceError,
    PromptTemplateValidationError,
)


prompt_templates_bp = Blueprint("prompt_templates", __name__)


@prompt_templates_bp.get("")
@prompt_templates_bp.get("/")
def list_prompt_templates():
    try:
        return _success(_template_service().list_templates())
    except Exception as exc:
        return _template_error(exc)


@prompt_templates_bp.post("")
@prompt_templates_bp.post("/")
def create_prompt_template():
    try:
        template = _template_service().create_template(_json_payload())
        return _success(template, status=201)
    except Exception as exc:
        return _template_error(exc)


@prompt_templates_bp.put("/<template_id>")
def update_prompt_template(template_id: str):
    try:
        template = _template_service().update_template(template_id, _json_payload())
        return _success(template)
    except Exception as exc:
        return _template_error(exc)


@prompt_templates_bp.delete("/<template_id>")
def delete_prompt_template(template_id: str):
    try:
        _template_service().delete_template(template_id)
        return _success({"deleted": True, "id": template_id})
    except Exception as exc:
        return _template_error(exc)


def _template_service() -> PromptTemplateService:
    service = current_app.config.get("PROMPT_TEMPLATE_SERVICE")
    if service is not None:
        return service
    template_path = current_app.config.get("PROMPT_TEMPLATE_PATH")
    return PromptTemplateService(template_path=template_path)


def _json_payload() -> dict[str, Any]:
    payload = request.get_json(silent=True)
    if payload is None:
        return {}
    if not isinstance(payload, dict):
        raise PromptTemplateValidationError("请求体必须是 JSON 对象")
    return payload


def _success(data: Any, status: int = 200):
    return jsonify({"success": True, "data": data}), status


def _template_error(exc: Exception):
    if isinstance(exc, PromptTemplateNotFoundError):
        return _error(str(exc).strip("'"), 404)
    if isinstance(exc, PromptTemplateValidationError):
        return _error(str(exc), 400)
    if isinstance(exc, PromptTemplateServiceError):
        return _error(str(exc), 500)
    current_app.logger.exception("Unhandled prompt-template route error")
    return _error("服务器内部错误", 500)


def _error(message: str, status: int, details: dict[str, Any] | None = None):
    return jsonify({"success": False, "error": {"message": message, "details": details or {}}}), status
