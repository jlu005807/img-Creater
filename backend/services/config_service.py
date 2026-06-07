from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[1] / "data" / "configs.json"
DEFAULT_MODEL = "gpt-image-2"
DEFAULT_API_TYPE = "openai"

# Upstream protocol per node. "openai" is the standard OpenAI-compatible
# Images API (/v1/images/generations, /v1/images/edits); "async" is a custom
# relay that exposes /async/images submit + poll.
_API_TYPE_ALIASES = {
    "openai": "openai",
    "openai-compatible": "openai",
    "openai_compatible": "openai",
    "compatible": "openai",
    "images": "openai",
    "sync": "openai",
    "async": "async",
    "relay": "async",
    "async_images": "async",
}


class ConfigServiceError(Exception):
    """Base error for JSON config persistence failures."""


class ConfigValidationError(ConfigServiceError, ValueError):
    """Raised when a config payload is incomplete or invalid."""


class ConfigNotFoundError(ConfigServiceError, KeyError):
    """Raised when a requested config id does not exist."""


class ConfigService:
    def __init__(self, config_path: str | Path | None = None):
        self.config_path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
        self._lock = threading.RLock()
        self._ensure_storage()

    def list_configs(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(item) for item in self._read_store()["configs"]]

    def get_config(self, config_id: str) -> dict[str, Any]:
        with self._lock:
            for config in self._read_store()["configs"]:
                if config["id"] == config_id:
                    return dict(config)
        raise ConfigNotFoundError(f"API 配置不存在: {config_id}")

    def get_enabled_configs(self) -> list[dict[str, Any]]:
        return [config for config in self.list_configs() if self.is_enabled(config)]

    def create_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            store = self._read_store()
            now = self._now()
            config = self._normalize_config(payload, existing=None)
            config["id"] = config.get("id") or uuid.uuid4().hex
            config["created_at"] = now
            config["updated_at"] = now
            store["configs"].append(config)
            self._write_store(store)
            return dict(config)

    def update_config(self, config_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        payload = dict(payload or {})
        # A blank/null api_key on update means "keep the existing key" — the
        # frontend never receives the real key back, so it can't resend it.
        # Note `str(None)` is "None" (truthy), so guard the None case explicitly.
        if "api_key" in payload and not str(payload.get("api_key") or "").strip():
            payload.pop("api_key")
        with self._lock:
            store = self._read_store()
            for index, current in enumerate(store["configs"]):
                if current["id"] == config_id:
                    updated = self._normalize_config(payload, existing=current)
                    updated["id"] = current["id"]
                    updated["created_at"] = current.get("created_at", self._now())
                    updated["updated_at"] = self._now()
                    store["configs"][index] = updated
                    self._write_store(store)
                    return dict(updated)
        raise ConfigNotFoundError(f"API 配置不存在: {config_id}")

    def delete_config(self, config_id: str) -> None:
        with self._lock:
            store = self._read_store()
            next_configs = [config for config in store["configs"] if config["id"] != config_id]
            if len(next_configs) == len(store["configs"]):
                raise ConfigNotFoundError(f"API 配置不存在: {config_id}")
            store["configs"] = next_configs
            self._write_store(store)

    def reorder_configs(self, ordered_ids: list[str]) -> list[dict[str, Any]]:
        with self._lock:
            store = self._read_store()
            current_by_id = {config["id"]: config for config in store["configs"]}
            current_ids = set(current_by_id)
            requested_ids = set(ordered_ids)

            if current_ids != requested_ids:
                missing = sorted(current_ids - requested_ids)
                unknown = sorted(requested_ids - current_ids)
                detail = f"missing={missing}, unknown={unknown}"
                raise ConfigValidationError(f"排序 ID 必须完整匹配现有配置: {detail}")

            # 列表顺序就是后续 Fallback 调度的优先级；拖拽排序只需要重写数组顺序。
            store["configs"] = [current_by_id[config_id] for config_id in ordered_ids]
            self._write_store(store)
            return [dict(item) for item in store["configs"]]

    @staticmethod
    def public_config(config: dict[str, Any]) -> dict[str, Any]:
        """A config view safe to send to the browser: the real api_key is
        dropped and replaced by a masked preview (last 4 chars)."""
        data = {key: value for key, value in config.items() if key != "api_key"}
        raw_key = str(config.get("api_key") or "")
        data["has_api_key"] = bool(raw_key)
        if len(raw_key) >= 4:
            data["api_key_preview"] = f"••••{raw_key[-4:]}"
        elif raw_key:
            data["api_key_preview"] = "••••"
        else:
            data["api_key_preview"] = ""
        return data

    @staticmethod
    def is_enabled(config: dict[str, Any]) -> bool:
        value = config.get("status", True)
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"enabled", "enable", "true", "1", "on"}

    def _ensure_storage(self) -> None:
        with self._lock:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            if not self.config_path.exists():
                self._write_store({"configs": []})

    def _read_store(self) -> dict[str, list[dict[str, Any]]]:
        try:
            raw = self.config_path.read_text(encoding="utf-8")
            data = json.loads(raw) if raw.strip() else {"configs": []}
        except json.JSONDecodeError as exc:
            raise ConfigServiceError(f"配置文件 JSON 格式错误: {self.config_path}") from exc
        except OSError as exc:
            raise ConfigServiceError(f"读取配置文件失败: {self.config_path}") from exc

        if isinstance(data, list):
            data = {"configs": data}
        if not isinstance(data, dict) or not isinstance(data.get("configs"), list):
            raise ConfigServiceError("配置文件结构必须为 {'configs': [...]}")
        return {"configs": [dict(item) for item in data["configs"] if isinstance(item, dict)]}

    def _write_store(self, store: dict[str, Any]) -> None:
        payload = json.dumps(store, ensure_ascii=False, indent=2)
        tmp_path = self.config_path.with_suffix(f"{self.config_path.suffix}.tmp")
        try:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path.write_text(payload + "\n", encoding="utf-8")
            tmp_path.replace(self.config_path)
        except OSError as exc:
            raise ConfigServiceError(f"写入配置文件失败: {self.config_path}") from exc

    def _normalize_config(
        self,
        payload: dict[str, Any],
        existing: dict[str, Any] | None,
    ) -> dict[str, Any]:
        merged = dict(existing or {})
        merged.update(payload or {})

        name = str(merged.get("name", "")).strip()
        base_url = str(merged.get("base_url", "")).strip().rstrip("/")
        api_key = str(merged.get("api_key", "")).strip()
        model = str(merged.get("model") or DEFAULT_MODEL).strip()
        status = self._normalize_status(merged.get("status", True))
        api_type = self._normalize_api_type(merged.get("api_type", DEFAULT_API_TYPE))

        if not name:
            raise ConfigValidationError("配置名称不能为空")
        if not base_url.startswith(("http://", "https://")):
            raise ConfigValidationError("base_url 必须以 http:// 或 https:// 开头")
        if not api_key:
            raise ConfigValidationError("api_key 不能为空")
        if not model:
            raise ConfigValidationError("model 不能为空")

        return {
            "id": merged.get("id"),
            "name": name,
            "base_url": base_url,
            "api_key": api_key,
            "model": model,
            "api_type": api_type,
            "status": status,
        }

    @staticmethod
    def _normalize_api_type(value: Any) -> str:
        text = str(value or DEFAULT_API_TYPE).strip().lower()
        resolved = _API_TYPE_ALIASES.get(text)
        if resolved is None:
            raise ConfigValidationError("api_type 只支持 openai 或 async")
        return resolved

    @staticmethod
    def _normalize_status(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in {"enabled", "enable", "true", "1", "on"}:
            return True
        if text in {"disabled", "disable", "false", "0", "off"}:
            return False
        raise ConfigValidationError("status 必须是启用/禁用状态")

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

