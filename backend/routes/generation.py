from __future__ import annotations

import base64
import binascii
import json
import math
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flask import Blueprint, current_app, jsonify, request

from backend.services.config_service import ConfigService
from backend.services.image_service import ImageService, ImageServiceError


generation_bp = Blueprint("generation", __name__)

_SESSION_DEFAULT_LIMIT = 30
_SESSION_MAX_LIMIT = 100
_SESSION_SORT = "updated_desc"
_SESSION_SUMMARY_PROMPT_MAX = 4000
_SESSION_LAST_STATUS_FIELDS = ("last_task_id", "last_status", "last_error")


class _SessionManifestError(ValueError):
    """A readable manifest that does not satisfy the session contract."""


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
            image=payload.get("image"),
            source_image=payload.get("source_image"),
            marked_image=payload.get("marked_image"),
            reference_images=payload.get("reference_images"),
            size=payload.get("size", "1024x1024"),
            n=payload.get("n", 1),
            quality=payload.get("quality"),
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


@generation_bp.post("/sessions/export")
def export_session_images():
    try:
        from backend.services.session_export_service import (
            SessionExportError,
            export_sessions,
        )

        result_dir = _result_dir()
        payload = request.get_json(silent=True)
        return export_sessions(current_app, result_dir, payload)
    except SessionExportError as exc:
        return _error(exc.message, exc.status_code, exc.details)
    except Exception as exc:
        return _image_error(exc)


@generation_bp.get("/sessions")
def list_sessions():
    try:
        options = _session_query_options()
        sessions = _load_session_records(_result_dir())
        sessions.sort(key=_session_sort_key, reverse=True)

        if options is None:
            return _success([record["legacy_item"] for record in sessions])

        query = options["q"].casefold()
        if query:
            sessions = [record for record in sessions if query in record["prompt"].casefold()]

        from_time = options["from"]
        if from_time is not None:
            sessions = [record for record in sessions if record["timestamp_value"] >= from_time]
        to_time = options["to"]
        if to_time is not None:
            sessions = [record for record in sessions if record["timestamp_value"] <= to_time]

        cursor = options["cursor"]
        if cursor is not None:
            cursor_key = (cursor["timestamp_value"], cursor["id"])
            sessions = [record for record in sessions if _session_sort_key(record) < cursor_key]

        limit = options["limit"]
        has_more = len(sessions) > limit
        page = sessions[:limit]
        next_cursor = _encode_session_cursor(page[-1]) if has_more else None
        return _success(
            {
                "items": [record["summary"] for record in page],
                "next_cursor": next_cursor,
                "has_more": has_more,
            }
        )
    except Exception as exc:
        return _image_error(exc)


@generation_bp.get("/sessions/<path:history_id>")
def get_session(history_id: str):
    """Return the full completed-session manifest for a history entry."""

    try:
        requested_history_id = history_id
        normalized_history_id = _normalize_history_id(history_id)
        result_dir = _result_dir()
        session_dir = _session_dir_for_public_id(result_dir, requested_history_id)
        if session_dir is None:
            raise ImageServiceError("Session not found", status_code=404)
        manifest_path = session_dir / "session.json"
        try:
            with manifest_path.open("r", encoding="utf-8-sig") as fh:
                manifest = json.load(fh)
            record = _session_record(manifest, session_dir.name, session_dir)
        except FileNotFoundError:
            raise ImageServiceError("Session not found", status_code=404) from None
        except (OSError, UnicodeError, json.JSONDecodeError, _SessionManifestError) as exc:
            current_app.logger.warning(
                "Unable to load session %s (%s)",
                _safe_session_log_identifier(normalized_history_id),
                type(exc).__name__,
            )
            raise ImageServiceError("Session not found", status_code=404) from exc

        if record is None:
            raise ImageServiceError("Session not found", status_code=404)
        return _success(record["legacy_item"])
    except Exception as exc:
        return _image_error(exc)


def _session_query_options() -> dict[str, Any] | None:
    if not request.args:
        return None

    raw_limit = _single_session_query_value("limit")
    if raw_limit is None:
        limit = _SESSION_DEFAULT_LIMIT
    else:
        try:
            limit = int(raw_limit)
        except (TypeError, ValueError) as exc:
            raise ImageServiceError("limit must be an integer between 1 and 100", status_code=400) from exc
        if not 1 <= limit <= _SESSION_MAX_LIMIT:
            raise ImageServiceError("limit must be an integer between 1 and 100", status_code=400)

    sort = _single_session_query_value("sort")
    if sort is not None and sort != _SESSION_SORT:
        raise ImageServiceError("sort must be updated_desc", status_code=400)

    from_time = _optional_session_query_time("from")
    to_time = _optional_session_query_time("to")
    if from_time is not None and to_time is not None and from_time > to_time:
        raise ImageServiceError("from must be earlier than or equal to to", status_code=400)

    raw_cursor = _single_session_query_value("cursor")
    cursor = _decode_session_cursor(raw_cursor) if raw_cursor is not None else None
    return {
        "limit": limit,
        "cursor": cursor,
        "q": _single_session_query_value("q") or "",
        "from": from_time,
        "to": to_time,
    }


def _single_session_query_value(name: str) -> str | None:
    values = request.args.getlist(name)
    if len(values) > 1:
        raise ImageServiceError(f"{name} must be provided once", status_code=400)
    return values[0] if values else None


def _optional_session_query_time(name: str) -> datetime | None:
    value = _single_session_query_value(name)
    if value is None:
        return None
    return _parse_iso8601(value, name)


def _parse_iso8601(value: Any, field_name: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ImageServiceError(f"{field_name} must be a valid ISO-8601 date", status_code=400)
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError) as exc:
        raise ImageServiceError(f"{field_name} must be a valid ISO-8601 date", status_code=400) from exc


def _load_session_records(result_dir) -> list[dict[str, Any]]:
    if not result_dir.exists() or not result_dir.is_dir():
        return []

    records = []
    seen_ids: dict[str, str] = {}
    # Directory order is not guaranteed by pathlib/filesystems.  Loading in
    # name order gives duplicate public IDs a deterministic winner and makes
    # the cursor tie-breaker stable across requests.
    try:
        session_dirs = sorted(result_dir.iterdir(), key=lambda path: path.name)
    except OSError as exc:
        current_app.logger.warning(
            "Unable to enumerate history directory (%s)",
            type(exc).__name__,
        )
        return []
    for session_dir in session_dirs:
        manifest_path = session_dir / "session.json"
        try:
            if not session_dir.is_dir() or not manifest_path.exists():
                continue
            with manifest_path.open("r", encoding="utf-8-sig") as fh:
                manifest = json.load(fh)
            record = _session_record(manifest, session_dir.name, session_dir)
        except (OSError, UnicodeError, json.JSONDecodeError, _SessionManifestError) as exc:
            # Keep diagnostics useful without exposing the server's absolute
            # manifest path.  The directory name plus exception class is
            # enough to identify a damaged legacy session safely.
            current_app.logger.warning(
                "Skipping unreadable session %s (%s)",
                session_dir.name,
                type(exc).__name__,
            )
            continue
        if record is not None:
            public_id = record["id"]
            if public_id in seen_ids:
                current_app.logger.warning(
                    "Skipping duplicate session id %s from directory %s (already loaded from %s)",
                    _safe_session_log_identifier(public_id),
                    session_dir.name,
                    seen_ids[public_id],
                )
                continue
            seen_ids[public_id] = session_dir.name
            records.append(record)
    return records


def _session_dir_for_public_id(result_dir: Path, public_id: str) -> Path | None:
    """Resolve a listed public ID without normalizing legacy manifest IDs."""

    for record in _load_session_records(result_dir):
        if record["id"] == public_id:
            return result_dir / record["directory_name"]
    return None


def _session_record(
    manifest: Any,
    directory_name: str,
    session_dir: Path | None = None,
) -> dict[str, Any] | None:
    if not isinstance(manifest, dict):
        raise _SessionManifestError("manifest must contain a JSON object")
    if manifest.get("status") != "completed":
        return None

    raw_urls = manifest.get("urls")
    if not isinstance(raw_urls, list):
        raise _SessionManifestError("completed manifest urls must be an array")
    urls = [url.strip() for url in raw_urls if isinstance(url, str) and url.strip()]
    if not urls:
        raise _SessionManifestError("completed manifest urls must contain a string URL")

    raw_id = manifest.get("id")
    session_id = raw_id if isinstance(raw_id, str) and raw_id else directory_name
    if session_dir is None:
        session_dir = Path(directory_name)
    timestamp, timestamp_value = _session_timestamp(manifest, session_dir)

    prompt = manifest.get("prompt") if isinstance(manifest.get("prompt"), str) else ""
    images = [
        {
            "index": index,
            "url": url,
            "filename": url.rsplit("/", 1)[-1],
            "session_id": session_id,
        }
        for index, url in enumerate(urls)
    ]
    reference_images = manifest.get("reference_images")
    if not isinstance(reference_images, list):
        reference_images = []
    else:
        reference_images = [url for url in reference_images if isinstance(url, str)]

    legacy_item = dict(manifest)
    legacy_item["id"] = session_id
    legacy_item["urls"] = urls
    legacy_item["images"] = images
    legacy_item["reference_images"] = reference_images
    legacy_item["updated_at"] = timestamp

    summary = {
        "id": session_id,
        "prompt": prompt[:_SESSION_SUMMARY_PROMPT_MAX],
        "mode": _safe_manifest_value(manifest.get("mode")),
        "size": _safe_manifest_value(manifest.get("size")),
        "n": _safe_manifest_value(manifest.get("n")),
        "status": "completed",
        "urls": urls,
        "images": images,
        "reference_images": reference_images,
        "api_id": _safe_manifest_value(manifest.get("api_id")),
        "api_name": _safe_manifest_value(manifest.get("api_name")),
        "task_id": _safe_manifest_value(manifest.get("task_id")),
        "expires_at": _safe_manifest_value(manifest.get("expires_at")),
        "created_at": _safe_manifest_value(manifest.get("created_at")),
        "updated_at": timestamp,
    }
    for field in _SESSION_LAST_STATUS_FIELDS:
        if field in manifest:
            summary[field] = _safe_manifest_value(manifest[field])

    return {
        "id": session_id,
        "directory_name": directory_name,
        "timestamp": timestamp,
        "timestamp_value": timestamp_value,
        "prompt": prompt,
        "legacy_item": legacy_item,
        "summary": summary,
    }


def _session_timestamp(manifest: dict[str, Any], session_dir: Path) -> tuple[str, datetime]:
    """Return the first valid manifest timestamp, or the directory mtime.

    Older sessions may have omitted timestamps, and interrupted writes can
    leave one timestamp malformed.  Such sessions remain listable by using
    the first parseable value and finally the directory's UTC modification
    time as a stable pagination key.
    """

    for field_name in ("updated_at", "created_at"):
        raw_value = manifest.get(field_name)
        try:
            parsed = _parse_iso8601(raw_value, f"session {field_name}")
        except ImageServiceError:
            continue
        return raw_value, parsed

    try:
        mtime = session_dir.stat().st_mtime
        if not math.isfinite(mtime):
            raise ValueError("session directory mtime is not finite")
        parsed = datetime.fromtimestamp(mtime, timezone.utc)
    except (OverflowError, ValueError) as exc:
        raise _SessionManifestError("session directory mtime is unavailable") from exc
    return parsed.isoformat().replace("+00:00", "Z"), parsed


def _safe_session_log_identifier(value: Any) -> str:
    """Keep identifiers in warnings path-safe even for malformed legacy IDs."""

    text = str(value).replace("\\", "/")
    return text.rsplit("/", 1)[-1] or "<empty>"


def _safe_manifest_value(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value if value is None or isinstance(value, (str, int, float, bool)) else None


def _session_sort_key(record: dict[str, Any]) -> tuple[datetime, str]:
    return record["timestamp_value"], record["id"]


def _encode_session_cursor(record: dict[str, Any]) -> str:
    payload = json.dumps([record["timestamp"], record["id"]], ensure_ascii=False, separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")


def _decode_session_cursor(cursor: Any) -> dict[str, Any]:
    if not isinstance(cursor, str) or not cursor or len(cursor) % 4 == 1:
        raise ImageServiceError("cursor is invalid", status_code=400)
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        decoded = base64.b64decode(padded, altchars=b"-_", validate=True)
        canonical = base64.urlsafe_b64encode(decoded).decode("ascii").rstrip("=")
        if canonical != cursor:
            raise ValueError("cursor encoding is not canonical")
        payload = json.loads(decoded.decode("utf-8"))
        if (
            not isinstance(payload, list)
            or len(payload) != 2
            or not isinstance(payload[0], str)
            or not isinstance(payload[1], str)
            or not payload[1]
        ):
            raise ValueError("cursor payload has the wrong shape")
        timestamp_value = _parse_iso8601(payload[0], "cursor timestamp")
    except (ValueError, UnicodeError, binascii.Error, ImageServiceError) as exc:
        raise ImageServiceError("cursor is invalid", status_code=400) from exc
    return {"timestamp_value": timestamp_value, "id": payload[1]}


@generation_bp.delete("/sessions/<path:history_id>")
def delete_session(history_id: str):
    try:
        requested_history_id = history_id
        normalized_history_id = _normalize_history_id(history_id)
        result_dir = _result_dir()
        session_dir = _session_dir_for_public_id(result_dir, requested_history_id)
        if session_dir is None and requested_history_id == normalized_history_id:
            session_dir = result_dir / normalized_history_id
        if session_dir is not None and session_dir.exists():
            shutil.rmtree(session_dir)
        return _success({"deleted": True, "id": normalized_history_id})
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


@generation_bp.get("/edit-drafts/<path:history_id>")
def get_edit_draft(history_id: str):
    try:
        draft_path = _edit_draft_path(history_id)
        if not draft_path.exists():
            return _success(None)
        with draft_path.open("r", encoding="utf-8-sig") as fh:
            return _success(json.load(fh))
    except Exception as exc:
        return _image_error(exc)


@generation_bp.put("/edit-drafts/<path:history_id>")
def save_edit_draft(history_id: str):
    try:
        draft_path = _edit_draft_path(history_id)
        payload = _json_payload()
        if not payload:
            raise ImageServiceError("局部编辑草稿不能为空", status_code=400)
        # 增量保存：payload 不带原图（image 缺失/为 null/为空）且磁盘上已有草稿时，
        # 把新字段合并到已存草稿上（保留原图和 payload 未携带的字段）。合并在服务端完成、
        # 落盘始终是完整草稿结构，因此 GET 对旧版整包草稿和合并后草稿的读取方式完全一致。
        # 磁盘上没有旧草稿时按原样存储（首次保存仍应由前端携带完整草稿）。
        if not payload.get("image") and draft_path.exists():
            try:
                with draft_path.open("r", encoding="utf-8-sig") as fh:
                    existing = json.load(fh)
            except (OSError, ValueError):
                existing = None
            if isinstance(existing, dict):
                incremental = dict(payload)
                # 空的 image 字段不能覆盖已存原图。
                incremental.pop("image", None)
                payload = {**existing, **incremental}
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
