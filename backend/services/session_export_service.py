from __future__ import annotations
import json
import re
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from tempfile import SpooledTemporaryFile
from typing import Any
from urllib.parse import unquote, urlparse
from PIL import Image, UnidentifiedImageError
from backend.routes.generation import _load_session_records
from backend.services.image_service import ImageServiceError
SESSION_EXPORT_MAX_ITEMS = 50
SESSION_EXPORT_MAX_FILE_BYTES = 50 * 1024 * 1024
SESSION_EXPORT_MAX_TOTAL_BYTES = 200 * 1024 * 1024
SESSION_EXPORT_SPOOL_MAX_BYTES = 8 * 1024 * 1024
_ALLOWED_EXTENSIONS = {"png": "PNG", "jpg": "JPEG", "jpeg": "JPEG", "webp": "WEBP"}
class SessionExportError(ImageServiceError):
    pass
class _ExportItem:
    __slots__ = ("session_id", "image_index")
    def __init__(self, session_id, image_index):
        self.session_id = session_id
        self.image_index = image_index
    @property
    def key(self):
        return (self.session_id, self.image_index)
def _config(app, key, default):
    return app.config.get(key, default)
def _validate_items(payload, max_items):
    if not isinstance(payload, dict):
        raise SessionExportError("Request body must be a JSON object", status_code=400)
    raw_items = payload.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise SessionExportError("items must be a non-empty array", status_code=400)
    if len(raw_items) > max_items:
        raise SessionExportError("Too many items", status_code=413)
    items = []
    seen = set()
    for entry in raw_items:
        if not isinstance(entry, dict):
            raise SessionExportError("Each item must be an object", status_code=400)
        if set(entry.keys()) != {"session_id", "image_index"}:
            raise SessionExportError("Each item must contain exactly session_id and image_index", status_code=400)
        session_id = entry["session_id"]
        image_index = entry["image_index"]
        if not isinstance(session_id, str) or not session_id.strip():
            raise SessionExportError("session_id must be a non-empty string", status_code=400)
        if isinstance(image_index, bool) or not isinstance(image_index, int):
            raise SessionExportError("image_index must be an integer", status_code=400)
        if image_index < 0:
            raise SessionExportError("image_index must be non-negative", status_code=400)
        item = _ExportItem(session_id.strip(), image_index)
        if item.key in seen:
            raise SessionExportError("Duplicate item", status_code=400)
        seen.add(item.key)
        items.append(item)
    return items
def _resolve_local_file(result_dir, session_dir, url, session_id):
    parsed = urlparse(str(url or ""))
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
        return None
    raw_path = unquote(parsed.path or "")
    prefix = "/api/results/"
    if not raw_path.startswith(prefix):
        return None
    relative = raw_path[len(prefix):].lstrip("/")
    if not relative:
        return None
    parts = relative.split("/")
    if len(parts) < 2:
        return None
    url_session_id = parts[0]
    filename = "/".join(parts[1:])
    if not filename:
        return None
    if url_session_id != session_dir.name:
        return None
    root = session_dir.resolve()
    candidate = (session_dir / filename).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    if not candidate.is_file():
        return None
    if candidate.is_symlink():
        try:
            target = candidate.resolve()
            target.relative_to(root)
        except ValueError:
            return None
    return candidate
def _validate_image(path):
    ext = path.suffix.lower().lstrip(".")
    expected_format = _ALLOWED_EXTENSIONS.get(ext)
    if expected_format is None:
        return None
    try:
        with Image.open(path) as img:
            img.verify()
        with Image.open(path) as img:
            actual_format = img.format
    except (UnidentifiedImageError, OSError, ValueError):
        return None
    if actual_format != expected_format:
        return None
    return actual_format
def _safe_zip_name(session_id, image_index, ext):
    safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", session_id)[:60] or "session"
    return f"{safe_id}_{image_index}_{uuid.uuid4().hex[:8]}.{ext}"
def _zip_filename():
    now = datetime.now(timezone.utc)
    return f"img-Creater-{now.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}.zip"
def _safe_error(text):
    sanitized = re.sub(r"[A-Za-z]:[\\/][^\s\"'<>]+", "<path>", text)
    sanitized = re.sub(r"https?://[^\s\"'<>]+", "<url>", sanitized)
    sanitized = re.sub(r"data:[^\s\"'<>]+", "<data-url>", sanitized)
    return sanitized[:300]
def _safe_skip(entry):
    return {"session_id": entry["session_id"], "image_index": entry["image_index"], "error": _safe_error(entry["error"])}
def build_export(app, result_dir, items):
    max_file_bytes = _config(app, "SESSION_EXPORT_MAX_FILE_BYTES", SESSION_EXPORT_MAX_FILE_BYTES)
    max_total_bytes = _config(app, "SESSION_EXPORT_MAX_TOTAL_BYTES", SESSION_EXPORT_MAX_TOTAL_BYTES)
    spool_max = _config(app, "SESSION_EXPORT_SPOOL_MAX_BYTES", SESSION_EXPORT_SPOOL_MAX_BYTES)
    records = _load_session_records(result_dir)
    id_to_dir = {}
    for record in records:
        id_to_dir[record["id"]] = result_dir / record["directory_name"]
    exported = []
    skipped = []
    total_bytes = 0
    spool = SpooledTemporaryFile(max_size=spool_max, suffix=".zip")
    archive = zipfile.ZipFile(spool, "w", zipfile.ZIP_STORED)
    try:
        for item in items:
            session_dir = id_to_dir.get(item.session_id)
            if session_dir is None:
                skipped.append({"session_id": item.session_id, "image_index": item.image_index, "error": "Session not found"})
                continue
            manifest_path = session_dir / "session.json"
            try:
                with manifest_path.open("r", encoding="utf-8-sig") as fh:
                    manifest = json.load(fh)
            except (OSError, json.JSONDecodeError):
                skipped.append({"session_id": item.session_id, "image_index": item.image_index, "error": "Session manifest unreadable"})
                continue
            urls = manifest.get("urls")
            if not isinstance(urls, list) or item.image_index >= len(urls):
                skipped.append({"session_id": item.session_id, "image_index": item.image_index, "error": "Image index out of range"})
                continue
            url = urls[item.image_index]
            if not isinstance(url, str):
                skipped.append({"session_id": item.session_id, "image_index": item.image_index, "error": "Invalid image URL in manifest"})
                continue
            local_path = _resolve_local_file(result_dir, session_dir, url, item.session_id)
            if local_path is None:
                skipped.append({"session_id": item.session_id, "image_index": item.image_index, "error": "Image is not a local file"})
                continue
            try:
                file_size = local_path.stat().st_size
            except OSError:
                skipped.append({"session_id": item.session_id, "image_index": item.image_index, "error": "File not accessible"})
                continue
            if file_size > max_file_bytes:
                archive.close()
                spool.close()
                raise SessionExportError("File exceeds size limit", status_code=413)
            if total_bytes + file_size > max_total_bytes:
                archive.close()
                spool.close()
                raise SessionExportError("Total export size exceeds limit", status_code=413)
            image_format = _validate_image(local_path)
            if image_format is None:
                skipped.append({"session_id": item.session_id, "image_index": item.image_index, "error": "Invalid or mismatched image file"})
                continue
            ext = image_format.lower()
            if ext == "jpeg":
                ext = "jpg"
            zip_name = _safe_zip_name(item.session_id, item.image_index, ext)
            try:
                with local_path.open("rb") as f:
                    archive.writestr(zip_name, f.read())
            except OSError:
                skipped.append({"session_id": item.session_id, "image_index": item.image_index, "error": "File read failed"})
                continue
            total_bytes += file_size
            exported.append({"session_id": item.session_id, "image_index": item.image_index, "filename": zip_name})
        if not exported:
            archive.close()
            spool.close()
            raise SessionExportError("All items failed to export", status_code=422, details={"skipped": [_safe_skip(s) for s in skipped]})
        report = {"exported": exported, "skipped": [_safe_skip(s) for s in skipped]}
        archive.writestr("export-report.json", json.dumps(report, ensure_ascii=False, indent=2))
        archive.close()
        return spool, _zip_filename(), len(skipped)
    except SessionExportError:
        raise
    except Exception:
        try:
            archive.close()
        except Exception:
            pass
        try:
            spool.close()
        except Exception:
            pass
        raise
def export_sessions(app, result_dir, payload):
    from flask import jsonify, send_file
    max_items = _config(app, "SESSION_EXPORT_MAX_ITEMS", SESSION_EXPORT_MAX_ITEMS)
    items = _validate_items(payload, max_items)
    spool, zip_name, skipped_count = build_export(app, result_dir, items)
    spool.seek(0)
    response = send_file(spool, mimetype="application/zip", as_attachment=True, download_name=zip_name)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Export-Skipped-Count"] = str(skipped_count)
    response.call_on_close(spool.close)
    return response
